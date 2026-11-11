"""稳定性：跨多次运行的同用例断言一致性（Flaky Rate）。

口径（设计文档 §7）：参与重跑（N ≥ 3）的用例中，任一断言结果不一致、
或状态在 passed 与 failed/error 之间摆动的用例数 / 参与重跑用例数。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select

from evalops.calibration import resolve_run_ref
from evalops.db.models import AssertionResult, Case, CaseResult, Run


class FlakyError(ValueError):
    """重跑数据不足或运行不存在。"""


@dataclass
class CaseFlake:
    case_key: str
    statuses: list[str]
    unstable_assertions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FlakyReport:
    run_ids: list[str]
    total_cases: int
    stable_cases: int
    flaky_cases: list[CaseFlake] = field(default_factory=list)
    skipped_cases: list[str] = field(default_factory=list)
    flaky_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["flaky_cases"] = [item.to_dict() for item in self.flaky_cases]
        return payload


async def compute_flaky(session: Any, run_ids: list[str]) -> FlakyReport:
    """按 ``case_key`` 对齐多次运行，判定断言结果是否稳定。"""
    ordered_ids = list(dict.fromkeys(run_ids))
    if len(ordered_ids) < 3:
        raise FlakyError("Flaky 判定至少需要三次运行（N ≥ 3）")
    for run_id in ordered_ids:
        if await session.get(Run, run_id) is None:
            raise FlakyError(f"运行不存在: {run_id}")

    rows = (
        await session.execute(
            select(CaseResult, Case.case_key)
            .join(Case, CaseResult.case_id == Case.id)
            .where(CaseResult.run_id.in_(ordered_ids))
        )
    ).all()
    by_case: dict[str, dict[str, CaseResult]] = {}
    for result, case_key in rows:
        by_case.setdefault(case_key, {})[result.run_id] = result

    result_ids = [result.id for result, _ in rows]
    assertions_by_result: dict[str, list[AssertionResult]] = {}
    if result_ids:
        assertions = (
            await session.scalars(
                select(AssertionResult).where(
                    AssertionResult.case_result_id.in_(result_ids)
                )
            )
        ).all()
        for assertion in assertions:
            assertions_by_result.setdefault(assertion.case_result_id, []).append(assertion)

    report = FlakyReport(run_ids=ordered_ids, total_cases=0, stable_cases=0)
    for case_key in sorted(by_case):
        run_results = by_case[case_key]
        present = [run_id for run_id in ordered_ids if run_id in run_results]
        if len(present) < 3:
            report.skipped_cases.append(case_key)
            continue

        report.total_cases += 1
        statuses = [run_results[run_id].status for run_id in present]
        signatures: dict[tuple[int, str], list[bool]] = {}
        for run_id in present:
            for assertion in assertions_by_result.get(run_results[run_id].id, []):
                signatures.setdefault((assertion.seq, assertion.type), []).append(
                    assertion.passed
                )

        unstable = [
            {"seq": seq, "type": kind, "values": values}
            for (seq, kind), values in sorted(signatures.items())
            if len(values) >= 2 and len(set(values)) > 1
        ]
        status_flip = "passed" in statuses and any(
            status != "passed" for status in statuses
        )
        if unstable or status_flip:
            report.flaky_cases.append(
                CaseFlake(
                    case_key=case_key,
                    statuses=statuses,
                    unstable_assertions=unstable,
                )
            )
        else:
            report.stable_cases += 1

    if report.total_cases:
        report.flaky_rate = len(report.flaky_cases) / report.total_cases
    return report


async def runs_for_group(session: Any, reference: str) -> list[Run]:
    """按重复运行组取回运行；``reference`` 可以是组 ID 或组内任一运行。"""
    anchor = await session.get(Run, reference)
    group = None
    if anchor is not None:
        group = (anchor.config_snapshot or {}).get("repeat_group") or anchor.id
    else:
        group = reference

    runs = (await session.scalars(select(Run).order_by(Run.started_at.asc()))).all()
    matched = [
        run for run in runs if (run.config_snapshot or {}).get("repeat_group") == group
    ]
    if not matched and anchor is not None:
        matched = [anchor]
    if not matched:
        raise FlakyError(f"找不到重复运行组: {reference}")
    matched.sort(
        key=lambda run: (
            int((run.config_snapshot or {}).get("repeat_index", 0)),
            run.started_at,
        )
    )
    return matched


async def resolve_run_refs(session: Any, references: str) -> list[Run]:
    """解析逗号分隔的运行 ID/前缀。"""
    parts = [item.strip() for item in references.split(",") if item.strip()]
    if not parts:
        raise FlakyError("--runs 不能为空")
    return [await resolve_run_ref(session, part) for part in parts]
