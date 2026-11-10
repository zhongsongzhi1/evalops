"""GitHub PR 触发与自动回评。

安全边界（设计文档 §19.2）：
- 校验 ``X-Hub-Signature-256``，密钥只从环境变量读取；
- 按 ``X-GitHub-Delivery`` 幂等，重投递不会重复跑；
- fork PR 不执行、不注入任何 secrets；
- 评测失败只更新事件状态，不让 webhook 返回 5xx 触发 GitHub 重试风暴。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from evalops.cases.dsl import load_project
from evalops.db.models import WebhookEvent
from evalops.db.session import Database
from evalops.regression import RegressionError, RegressionReport, compare_runs, load_gate
from evalops.runner import execute_run

ACTION_WHITELIST = ("opened", "synchronize", "reopened")


@dataclass
class WebhookSettings:
    secret: str = ""
    token: str = ""
    project: Path | None = None
    cases: Path | None = None
    gate_file: Path | None = None
    baseline: str = "baseline"
    api_url: str = "https://api.github.com"

    @classmethod
    def from_env(cls) -> WebhookSettings:
        project = os.environ.get("EVALOPS_PROJECT")
        cases = os.environ.get("EVALOPS_CASES")
        gate_file = os.environ.get("EVALOPS_GATE_FILE")
        return cls(
            secret=os.environ.get("EVALOPS_GITHUB_SECRET", ""),
            token=os.environ.get("EVALOPS_GITHUB_TOKEN", ""),
            project=Path(project) if project else None,
            cases=Path(cases) if cases else None,
            gate_file=Path(gate_file) if gate_file else None,
            baseline=os.environ.get("EVALOPS_BASELINE", "baseline"),
            api_url=os.environ.get("EVALOPS_GITHUB_API_URL", "https://api.github.com"),
        )


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def payload_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def claim_delivery(
    db: Database, delivery_id: str, event: str, digest: str
) -> bool:
    """登记投递；已存在则返回 False（幂等去重）。"""
    async with db.sessionmaker() as session:
        if await session.get(WebhookEvent, delivery_id) is not None:
            return False
        session.add(
            WebhookEvent(
                delivery_id=delivery_id,
                event=event,
                payload_digest=digest,
                status="received",
            )
        )
        try:
            await session.commit()
        except Exception:  # noqa: BLE001 - 并发重复投递时以先到者为准
            await session.rollback()
            return False
    return True


async def _update_delivery(
    db: Database, delivery_id: str, status: str, run_id: str | None = None
) -> None:
    async with db.sessionmaker() as session:
        event = await session.get(WebhookEvent, delivery_id)
        if event is None:
            return
        event.status = status
        if run_id is not None:
            event.run_id = run_id
        await session.commit()


def render_comment(report: RegressionReport) -> str:
    """把回归结论渲染成 PR 评论 Markdown。"""
    lines = [
        f"## EvalOps 回归门禁：{report.verdict.upper()}",
        "",
        f"- 基线：`{report.baseline_run_id}`，Pass Rate {report.baseline_pass_rate:.1%}",
        f"- 候选：`{report.candidate_run_id}`，Pass Rate {report.candidate_pass_rate:.1%}",
        f"- 变化：{report.delta:+.1%}",
        f"- 成本变化：{report.cost_delta_ratio:+.1%}",
    ]
    if report.judge_kappa is not None:
        lines.append(
            f"- Judge κ：{report.judge_kappa:.2f}"
            f"（加权 {report.judge_kappa_weighted or 0:.2f}，n={report.judge_kappa_n}）"
        )
    if report.new_failures:
        lines.extend(["", f"### New Failures（{len(report.new_failures)}）"])
        lines.extend(f"- `{key}`" for key in report.new_failures)
    if report.fixed:
        lines.extend(["", f"### 已修复（{len(report.fixed)}）"])
        lines.extend(f"- `{key}`" for key in report.fixed)
    if report.added:
        lines.extend(["", f"新增用例 {len(report.added)} 条（不计入 New Failures）"])
    if report.removed:
        lines.extend([f"移除用例 {len(report.removed)} 条：{', '.join(report.removed)}"])
    lines.extend(["", "### 判定依据"])
    lines.extend(f"- {reason}" for reason in report.reasons)
    if report.notes:
        lines.extend(f"- 注：{note}" for note in report.notes)
    lines.append("")
    lines.append("_由 EvalOps 自动生成_")
    return "\n".join(lines)


async def post_pr_comment(
    settings: WebhookSettings, repo: str, number: int, body: str
) -> bool:
    if not settings.token:
        return False
    url = f"{settings.api_url.rstrip('/')}/repos/{repo}/issues/{number}/comments"
    headers = {
        "Authorization": f"Bearer {settings.token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json={"body": body}, headers=headers)
    except httpx.HTTPError:
        return False
    return response.status_code < 400


async def process_pull_request(
    db: Database,
    settings: WebhookSettings,
    payload: dict[str, Any],
    delivery_id: str,
) -> dict[str, Any]:
    """执行一次候选评测、判门禁并尝试回评。"""
    pull_request = payload.get("pull_request") or {}
    head = pull_request.get("head") or {}
    if (head.get("repo") or {}).get("fork"):
        await _update_delivery(db, delivery_id, "skipped_fork")
        return {"status": "skipped", "reason": "fork_pr"}

    if settings.project is None or settings.cases is None:
        await _update_delivery(db, delivery_id, "skipped_unconfigured")
        return {"status": "skipped", "reason": "EVALOPS_PROJECT / EVALOPS_CASES 未配置"}

    repo = (payload.get("repository") or {}).get("full_name") or (
        (pull_request.get("base") or {}).get("repo") or {}
    ).get("full_name")
    number = pull_request.get("number") or payload.get("number")
    head_sha = head.get("sha")

    try:
        loaded = load_project(settings.project)
        outcome = await execute_run(
            db, loaded, settings.cases, trigger="ci", git_sha=head_sha
        )
        report = await compare_runs(
            db, settings.baseline, outcome.run_id, load_gate(settings.gate_file)
        )
    except RegressionError as exc:
        # 首次接入还没有基线：只记录运行结果，不阻断 PR
        await _update_delivery(db, delivery_id, "no_baseline")
        if repo and number and settings.token:
            await post_pr_comment(
                settings,
                str(repo),
                int(number),
                "## EvalOps 回归门禁：暂无基线\n\n"
                f"首次运行已记录，尚未设置基线：{exc}",
            )
        return {"status": "no_baseline", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 后台任务必须落状态
        await _update_delivery(db, delivery_id, "error")
        return {"status": "error", "reason": str(exc)}

    posted = False
    if repo and number and settings.token:
        posted = await post_pr_comment(
            settings, str(repo), int(number), render_comment(report)
        )

    await _update_delivery(db, delivery_id, report.verdict, report.candidate_run_id)
    return {
        "status": report.verdict,
        "run_id": report.candidate_run_id,
        "new_failures": report.new_failures,
        "comment_posted": posted,
    }
