"""GitHub webhook：签名校验、幂等、fork 防护与 PR 回评内容。"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response

from evalops.api.app import create_app
from evalops.config import resolve_db_url
from evalops.db.models import WebhookEvent
from evalops.db.session import Database
from evalops.github import (
    WebhookSettings,
    post_pr_comment,
    render_comment,
    verify_signature,
)
from evalops.regression import RegressionReport

SECRET = "test-webhook-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _payload(*, fork: bool = False, action: str = "opened") -> dict[str, Any]:
    head_repo = {"full_name": "acme/demo", "fork": fork}
    return {
        "action": action,
        "number": 7,
        "repository": {"full_name": "acme/demo"},
        "pull_request": {
            "number": 7,
            "head": {"sha": "deadbeef", "repo": head_repo},
            "base": {"repo": {"full_name": "acme/demo"}},
        },
    }


@pytest.fixture()
async def client(workdir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EVALOPS_GITHUB_SECRET", SECRET)
    monkeypatch.setenv("EVALOPS_PROJECT", str(workdir / "projects" / "fake.yaml"))
    monkeypatch.setenv("EVALOPS_CASES", str(workdir / "cases"))
    monkeypatch.delenv("EVALOPS_GITHUB_TOKEN", raising=False)
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            http.workdir = workdir
            yield http


async def _post(
    client: AsyncClient,
    payload: dict[str, Any],
    delivery: str,
    *,
    secret: str = SECRET,
) -> Response:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": _sign(body, secret),
    }
    return await client.post("/webhooks/github", content=body, headers=headers)


async def _run_baseline(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/runs",
        json={
            "project": str(client.workdir / "projects" / "fake.yaml"),
            "cases": str(client.workdir / "cases"),
            "baseline": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_signature_verification() -> None:
    body = b'{"action":"opened"}'
    assert verify_signature(SECRET, body, _sign(body)) is True
    assert verify_signature(SECRET, body, _sign(body, "wrong")) is False
    assert verify_signature(SECRET, body, None) is False


async def test_bad_signature_returns_401(client: AsyncClient) -> None:
    response = await _post(client, _payload(), "delivery-bad", secret="wrong")
    assert response.status_code == 401
    assert (await client.get("/api/runs")).json() == []


async def test_fork_pr_is_ignored(client: AsyncClient) -> None:
    response = await _post(client, _payload(fork=True), "delivery-fork")
    assert response.status_code == 202
    assert response.json()["reason"] == "fork_pr"
    assert (await client.get("/api/runs")).json() == []


async def test_webhook_runs_once_and_dedupes(client: AsyncClient) -> None:
    await _run_baseline(client)

    first = await _post(client, _payload(), "delivery-1")
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "accepted"

    runs = (await client.get("/api/runs")).json()
    assert len(runs) == 2

    database = Database.create(resolve_db_url())
    try:
        async with database.sessionmaker() as session:
            event = await session.get(WebhookEvent, "delivery-1")
            assert event is not None
            assert event.status == "pass"
            assert event.run_id is not None
    finally:
        await database.dispose()

    duplicate = await _post(client, _payload(), "delivery-1")
    assert duplicate.status_code == 202
    assert duplicate.json()["status"] == "duplicate"
    assert len((await client.get("/api/runs")).json()) == 2


def test_render_comment_contains_failures_and_kappa() -> None:
    report = RegressionReport(
        baseline_run_id="base",
        candidate_run_id="cand",
        project_id="p1",
        baseline_pass_rate=1.0,
        candidate_pass_rate=0.75,
        delta=-0.25,
        baseline_assertion_pass_rate=1.0,
        candidate_assertion_pass_rate=0.8,
        cost_delta_ratio=0.1,
        judge_kappa=0.72,
        judge_kappa_weighted=0.8,
        judge_kappa_n=60,
        new_failures=["refund_policy"],
        verdict="block",
        reasons=["New Failures 1 条，超过允许的 0 条"],
    )
    body = render_comment(report)
    assert "BLOCK" in body
    assert "`refund_policy`" in body
    assert "Judge κ：0.72" in body


async def test_post_comment_skipped_without_token() -> None:
    settings = WebhookSettings(secret=SECRET, token="")
    assert await post_pr_comment(settings, "acme/demo", 7, "body") is False
