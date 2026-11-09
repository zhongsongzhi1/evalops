"""执行器：用例 → adapter → 轨迹 → 断言 → 落库。

阶段 0 为串行执行；并发度、断点续跑与重试在阶段 1/3 接入。
断言失败不自动重试，避免掩盖 flaky（设计文档 §19.3）。
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from sqlalchemy import func, select

from evalops.adapters import AgentAdapter, Trajectory, build_adapter
from evalops.assertions import AssertionOutcome, evaluate_assertions
from evalops.cases.dsl import DslError, LoadedProject, filter_cases, load_cases
from evalops.db.models import (
    AssertionResult,
    Baseline,
    Case,
    CaseResult,
    CaseSet,
    Project,
    Run,
)
from evalops.db.session import Database


@dataclass
class CaseOutcome:
    case_key: str
    name: str
    status: str
    latency_ms: int = 0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None
    failed_assertions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RunOutcome:
    run_id: str
    project_id: str
    case_set_id: str
    status: str
    total: int
    passed: int
    failed: int
    errored: int
    pass_rate: float
    assertion_passed: int
    assertion_total: int
    assertion_pass_rate: float
    cost_usd: float
    duration_ms: int
    cases: list[CaseOutcome]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _prices(project: LoadedProject) -> tuple[float, float]:
    config = project.spec.config
    return (
        float(config.get("price_in_per_1k", 0.0)),
        float(config.get("price_out_per_1k", 0.0)),
    )


def compute_cost(trajectory: Trajectory, prices: tuple[float, float]) -> float:
    price_in, price_out = prices
    cost = trajectory.tokens_in / 1000 * price_in + trajectory.tokens_out / 1000 * price_out
    return round(cost, 8)


async def _execute_case(
    adapter: AgentAdapter,
    project: LoadedProject,
    case: Any,
    timeout_s: float,
    prices: tuple[float, float],
) -> tuple[CaseOutcome, list[dict[str, Any]], list[AssertionOutcome]]:
    started = perf_counter()
    try:
        trajectory = await asyncio.wait_for(
            adapter.run(case, project.spec.config), timeout=timeout_s
        )
    except TimeoutError:
        elapsed = int((perf_counter() - started) * 1000)
        return (
            CaseOutcome(
                case_key=case.case_key,
                name=case.name,
                status="error",
                latency_ms=elapsed,
                error=f"用例超时（>{timeout_s:g}s）",
            ),
            [],
            [],
        )
    except Exception as exc:  # noqa: BLE001 - adapter 失败统一记为用例错误
        elapsed = int((perf_counter() - started) * 1000)
        return (
            CaseOutcome(
                case_key=case.case_key,
                name=case.name,
                status="error",
                latency_ms=elapsed,
                error=str(exc),
            ),
            [],
            [],
        )

    cost = compute_cost(trajectory, prices)
    assertions = evaluate_assertions(case, trajectory, cost_usd=cost)
    passed = all(item.passed for item in assertions)
    missing_latency = int((perf_counter() - started) * 1000)
    outcome = CaseOutcome(
        case_key=case.case_key,
        name=case.name,
        status="passed" if passed else "failed",
        latency_ms=trajectory.latency_ms or missing_latency,
        cost_usd=cost,
        tokens_in=trajectory.tokens_in,
        tokens_out=trajectory.tokens_out,
        failed_assertions=[
            {"seq": item.seq, "type": item.type, "detail": item.detail}
            for item in assertions
            if not item.passed
        ],
    )
    return outcome, trajectory.to_dicts(), assertions


async def _upsert_project(
    session: Any, project: LoadedProject, loaded: Any
) -> tuple[Project, CaseSet, dict[str, str]]:
    spec = project.spec
    project_row = await session.scalar(select(Project).where(Project.name == spec.name))
    if project_row is None:
        project_row = Project(name=spec.name, adapter=spec.adapter, config=dict(spec.config))
        session.add(project_row)
        await session.flush()
    else:
        project_row.adapter = spec.adapter
        project_row.config = dict(spec.config)

    case_set_row = await session.scalar(
        select(CaseSet).where(
            CaseSet.project_id == project_row.id,
            CaseSet.content_hash == loaded.content_hash,
        )
    )
    if case_set_row is None:
        version = (
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(CaseSet)
                    .where(CaseSet.project_id == project_row.id)
                )
                or 0
            )
            + 1
        )
        case_set_row = CaseSet(
            project_id=project_row.id, version=version, content_hash=loaded.content_hash
        )
        session.add(case_set_row)
        await session.flush()
        for case in loaded.cases:
            session.add(
                Case(
                    case_set_id=case_set_row.id,
                    case_key=case.case_key,
                    name=case.name,
                    input=dict(case.input),
                    expected=case.expected.model_dump(mode="json"),
                    assertions=[
                        item.model_dump(mode="json") for item in case.compiled_assertions()
                    ],
                    tags=list(case.tags),
                    enabled=case.enabled,
                )
            )
        await session.flush()

    rows = (await session.scalars(select(Case).where(Case.case_set_id == case_set_row.id))).all()
    return project_row, case_set_row, {row.case_key: row.id for row in rows}


async def _persist_case_result(
    session: Any,
    run_id: str,
    case_id: str,
    outcome: CaseOutcome,
    trajectory: list[dict[str, Any]],
    assertions: list[AssertionOutcome],
) -> None:
    score = None if outcome.status == "error" else (1.0 if outcome.status == "passed" else 0.0)
    result = CaseResult(
        run_id=run_id,
        case_id=case_id,
        status=outcome.status,
        score=score,
        latency_ms=outcome.latency_ms,
        tokens_in=outcome.tokens_in,
        tokens_out=outcome.tokens_out,
        cost_usd=outcome.cost_usd,
        trajectory=trajectory,
        error=outcome.error,
    )
    session.add(result)
    await session.flush()
    for item in assertions:
        session.add(
            AssertionResult(
                case_result_id=result.id,
                seq=item.seq,
                type=item.type,
                passed=item.passed,
                score=item.score,
                detail=item.detail,
            )
        )
    await session.commit()


async def execute_run(
    db: Database,
    project: LoadedProject,
    cases_path: str | Path,
    *,
    tags: list[str] | None = None,
    trigger: str = "manual",
    git_sha: str | None = None,
    prompt_version: str | None = None,
    mark_baseline: bool = False,
    baseline_label: str = "latest",
) -> RunOutcome:
    loaded = load_cases(cases_path)
    selected = filter_cases(loaded.cases, tags)
    if not selected:
        raise DslError("标签过滤后没有可执行用例")

    adapter = build_adapter(project)
    prices = _prices(project)
    timeout_s = float(project.spec.config.get("case_timeout_s", 30))
    started = perf_counter()
    outcomes: list[CaseOutcome] = []
    assertion_passed = 0
    assertion_total = 0

    async with db.sessionmaker() as session:
        project_row, case_set_row, case_ids = await _upsert_project(session, project, loaded)
        run = Run(
            project_id=project_row.id,
            case_set_id=case_set_row.id,
            git_sha=git_sha,
            model=str(project.spec.config["model"]) if "model" in project.spec.config else None,
            prompt_version=prompt_version,
            trigger=trigger,
            config_snapshot=dict(project.spec.config),
            status="running",
        )
        session.add(run)
        await session.commit()
        run_id = run.id

        for case in selected:
            outcome, trajectory, assertions = await _execute_case(
                adapter, project, case, timeout_s, prices
            )
            outcomes.append(outcome)
            assertion_passed += sum(1 for item in assertions if item.passed)
            assertion_total += len(assertions)
            await _persist_case_result(
                session, run_id, case_ids[case.case_key], outcome, trajectory, assertions
            )
            run.cost_usd = round(run.cost_usd + outcome.cost_usd, 8)
            await session.commit()

        errored = sum(1 for item in outcomes if item.status == "error")
        run.status = "failed" if errored else "succeeded"
        run.finished_at = datetime.now(UTC)
        if mark_baseline:
            session.add(
                Baseline(project_id=project_row.id, run_id=run_id, label=baseline_label)
            )
        await session.commit()
        status = run.status

    total = len(outcomes)
    passed = sum(1 for item in outcomes if item.status == "passed")
    failed = sum(1 for item in outcomes if item.status == "failed")
    return RunOutcome(
        run_id=run_id,
        project_id=project_row.id,
        case_set_id=case_set_row.id,
        status=status,
        total=total,
        passed=passed,
        failed=failed,
        errored=sum(1 for item in outcomes if item.status == "error"),
        pass_rate=passed / total if total else 0.0,
        assertion_passed=assertion_passed,
        assertion_total=assertion_total,
        assertion_pass_rate=assertion_passed / assertion_total if assertion_total else 0.0,
        cost_usd=sum(item.cost_usd for item in outcomes),
        duration_ms=int((perf_counter() - started) * 1000),
        cases=outcomes,
    )
