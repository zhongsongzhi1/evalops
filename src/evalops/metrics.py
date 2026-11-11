"""运行级指标：Pass Rate、断言通过率、p95 延迟与单用例成本。"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select

from evalops.db.models import AssertionResult, CaseResult, Run


@dataclass
class RunMetrics:
    run_id: str
    status: str
    started_at: str | None
    total: int
    passed: int
    failed: int
    errored: int
    pass_rate: float
    assertion_passed: int
    assertion_total: int
    assertion_pass_rate: float
    cost_usd: float
    cost_per_case: float
    p95_latency_ms: int
    tokens_in: int
    tokens_out: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def percentile(values: list[int], ratio: float) -> float:
    """最近秩法分位数；空列表返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(ratio * len(ordered)) - 1)
    return float(ordered[min(index, len(ordered) - 1)])


async def compute_run_metrics(session: Any, run_id: str) -> RunMetrics | None:
    run = await session.get(Run, run_id)
    if run is None:
        return None
    results = (
        await session.scalars(select(CaseResult).where(CaseResult.run_id == run_id))
    ).all()
    assertion_values = (
        await session.scalars(
            select(AssertionResult.passed)
            .join(CaseResult, AssertionResult.case_result_id == CaseResult.id)
            .where(CaseResult.run_id == run_id)
        )
    ).all()

    total = len(results)
    passed = sum(1 for item in results if item.status == "passed")
    cost_usd = round(sum(item.cost_usd for item in results), 8)
    assertion_total = len(assertion_values)
    assertion_passed = sum(1 for value in assertion_values if value)
    return RunMetrics(
        run_id=run.id,
        status=run.status,
        started_at=run.started_at.isoformat() if run.started_at else None,
        total=total,
        passed=passed,
        failed=sum(1 for item in results if item.status == "failed"),
        errored=sum(1 for item in results if item.status == "error"),
        pass_rate=passed / total if total else 0.0,
        assertion_passed=assertion_passed,
        assertion_total=assertion_total,
        assertion_pass_rate=(
            assertion_passed / assertion_total if assertion_total else 0.0
        ),
        cost_usd=cost_usd,
        cost_per_case=cost_usd / total if total else 0.0,
        p95_latency_ms=int(percentile([item.latency_ms for item in results], 0.95)),
        tokens_in=sum(item.tokens_in for item in results),
        tokens_out=sum(item.tokens_out for item in results),
    )


async def project_trend(
    session: Any, project_id: str, limit: int = 20
) -> list[RunMetrics]:
    """按时间升序返回最近 ``limit`` 次运行指标。"""
    run_ids = (
        await session.scalars(
            select(Run.id)
            .where(Run.project_id == project_id)
            .order_by(Run.started_at.desc(), Run.id.desc())
            .limit(limit)
        )
    ).all()
    trend: list[RunMetrics] = []
    for run_id in run_ids:
        item = await compute_run_metrics(session, run_id)
        if item is not None:
            trend.append(item)
    return list(reversed(trend))
