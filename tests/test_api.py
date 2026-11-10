"""控制面 API 冒烟测试：触发运行、查询详情、基线对比。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from evalops.api.app import create_app


@pytest.fixture()
async def client(workdir: Path):
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            http.workdir = workdir
            yield http


async def _run_project(client: AsyncClient, workdir: Path, *, baseline: bool = False) -> dict:
    response = await client.post(
        "/api/runs",
        json={
            "project": str(workdir / "projects" / "fake.yaml"),
            "cases": str(workdir / "cases"),
            "baseline": baseline,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_run_list_detail_and_compare(client: AsyncClient) -> None:
    outcome = await _run_project(client, client.workdir, baseline=True)
    assert outcome["pass_rate"] == 1.0

    runs = (await client.get("/api/runs")).json()
    assert len(runs) == 1
    assert runs[0]["id"] == outcome["run_id"]
    assert runs[0]["passed"] == 4

    detail = (await client.get(f"/api/runs/{outcome['run_id']}")).json()
    assert detail["total"] == 4
    assert {case["status"] for case in detail["cases"]} == {"passed"}

    report = (await client.get(f"/api/compare?candidate={outcome['run_id']}")).json()
    assert report["verdict"] == "pass"
    assert report["new_failures"] == []


async def test_gate_blocks_after_fixture_regression(client: AsyncClient) -> None:
    workdir: Path = client.workdir
    baseline = await _run_project(client, workdir, baseline=True)

    fixture = workdir / "fixtures" / "refund_policy.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["output"] = "您的课程已购买 12 天，可以申请退款，请通过客服提交。"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    candidate = await _run_project(client, workdir)
    report = (await client.get(f"/api/compare?candidate={candidate['run_id']}")).json()
    assert report["verdict"] == "block"
    assert report["new_failures"] == ["refund_policy"]
    assert report["baseline_run_id"] == baseline["run_id"]


async def test_static_console_served(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert "EvalOps" in response.text


async def test_missing_run_returns_404(client: AsyncClient) -> None:
    assert (await client.get("/api/runs/nope")).status_code == 404


async def test_bad_project_path_returns_400(client: AsyncClient) -> None:
    response = await client.post("/api/runs", json={"project": "does/not/exist.yaml", "cases": "."})
    assert response.status_code == 400
