"""轨迹回放与两次运行 diff：按 ``case_key`` 对齐，逐步对比。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from evalops.db.models import AssertionResult, Case, CaseResult, Run

STEP_FIELDS = ("type", "name", "input", "output", "tokens", "latency_ms")


class DiffError(ValueError):
    """运行不存在或不属于同一项目。"""


def _brief(value: Any, limit: int = 240) -> Any:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, str):
        text = value
    else:
        return value
    return text if len(text) <= limit else f"{text[:limit]}…"


def diff_steps(
    baseline: list[dict[str, Any]], candidate: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按下标对齐轨迹步骤，返回变化列表。"""
    diffs: list[dict[str, Any]] = []
    for index in range(max(len(baseline), len(candidate))):
        if index >= len(baseline):
            diffs.append(
                {
                    "step": candidate[index].get("step", index + 1),
                    "status": "added",
                    "changes": [],
                }
            )
            continue
        if index >= len(candidate):
            diffs.append(
                {
                    "step": baseline[index].get("step", index + 1),
                    "status": "removed",
                    "changes": [],
                }
            )
            continue

        base_step = baseline[index]
        cand_step = candidate[index]
        changes = [
            {
                "field": field,
                "baseline": _brief(base_step.get(field)),
                "candidate": _brief(cand_step.get(field)),
            }
            for field in STEP_FIELDS
            if base_step.get(field) != cand_step.get(field)
        ]
        diffs.append(
            {
                "step": cand_step.get("step", index + 1),
                "status": "changed" if changes else "same",
                "changes": changes,
            }
        )
    return diffs


async def _load_run_cases(
    session: Any, run_id: str
) -> dict[str, dict[str, Any]]:
    rows = (
        await session.execute(
            select(CaseResult, Case.case_key)
            .join(Case, CaseResult.case_id == Case.id)
            .where(CaseResult.run_id == run_id)
        )
    ).all()
    items: dict[str, dict[str, Any]] = {}
    key_by_result: dict[str, str] = {}
    result_ids: list[str] = []
    for result, case_key in rows:
        items[case_key] = {"result": result, "assertions": []}
        key_by_result[result.id] = case_key
        result_ids.append(result.id)
    if result_ids:
        assertions = (
            await session.scalars(
                select(AssertionResult).where(
                    AssertionResult.case_result_id.in_(result_ids)
                )
            )
        ).all()
        for assertion in assertions:
            key = key_by_result.get(assertion.case_result_id)
            if key is not None:
                items[key]["assertions"].append(assertion)
    return items


def _assertion_diff(
    baseline: list[AssertionResult], candidate: list[AssertionResult]
) -> list[dict[str, Any]]:
    base_map = {(item.seq, item.type): item for item in baseline}
    cand_map = {(item.seq, item.type): item for item in candidate}
    changes: list[dict[str, Any]] = []
    for signature in sorted(set(base_map) | set(cand_map)):
        base_item = base_map.get(signature)
        cand_item = cand_map.get(signature)
        same = (
            base_item is not None
            and cand_item is not None
            and base_item.passed == cand_item.passed
            and base_item.score == cand_item.score
        )
        if same:
            continue
        changes.append(
            {
                "seq": signature[0],
                "type": signature[1],
                "baseline": None
                if base_item is None
                else {"passed": base_item.passed, "score": base_item.score},
                "candidate": None
                if cand_item is None
                else {"passed": cand_item.passed, "score": cand_item.score},
            }
        )
    return changes


async def diff_runs(
    session: Any,
    baseline_run_id: str,
    candidate_run_id: str,
    case_key: str | None = None,
) -> dict[str, Any]:
    baseline_run = await session.get(Run, baseline_run_id)
    candidate_run = await session.get(Run, candidate_run_id)
    if baseline_run is None or candidate_run is None:
        raise DiffError("基线或候选运行不存在")
    if baseline_run.project_id != candidate_run.project_id:
        raise DiffError("基线与候选不属于同一个项目")

    baseline_cases = await _load_run_cases(session, baseline_run_id)
    candidate_cases = await _load_run_cases(session, candidate_run_id)
    keys = (
        [case_key]
        if case_key is not None
        else sorted(set(baseline_cases) | set(candidate_cases))
    )
    if (
        case_key is not None
        and case_key not in baseline_cases
        and case_key not in candidate_cases
    ):
        raise DiffError(f"运行中没有用例: {case_key}")

    detailed = case_key is not None
    entries: list[dict[str, Any]] = []
    changed_cases = 0
    for key in keys:
        base_item = baseline_cases.get(key)
        cand_item = candidate_cases.get(key)
        base_result = base_item["result"] if base_item else None
        cand_result = cand_item["result"] if cand_item else None

        assertion_changes = _assertion_diff(
            base_item["assertions"] if base_item else [],
            cand_item["assertions"] if cand_item else [],
        )
        steps = diff_steps(
            base_result.trajectory if base_result else [],
            cand_result.trajectory if cand_result else [],
        )
        changed_steps = [item for item in steps if item["status"] != "same"]
        status_changed = (base_result.status if base_result else None) != (
            cand_result.status if cand_result else None
        )
        changed = bool(status_changed or assertion_changes or changed_steps)
        if changed:
            changed_cases += 1

        entry: dict[str, Any] = {
            "case_key": key,
            "changed": changed,
            "baseline_status": base_result.status if base_result else None,
            "candidate_status": cand_result.status if cand_result else None,
            "latency_ms": {
                "baseline": base_result.latency_ms if base_result else None,
                "candidate": cand_result.latency_ms if cand_result else None,
            },
            "cost_usd": {
                "baseline": base_result.cost_usd if base_result else None,
                "candidate": cand_result.cost_usd if cand_result else None,
            },
            "assertion_changes": assertion_changes,
            "step_changes": len(changed_steps),
        }
        if detailed:
            entry["steps"] = steps
        entries.append(entry)

    return {
        "baseline_run_id": baseline_run_id,
        "candidate_run_id": candidate_run_id,
        "detailed": detailed,
        "changed_cases": changed_cases,
        "cases": entries,
    }


async def load_case_trace(
    session: Any, run_id: str, case_key: str
) -> dict[str, Any] | None:
    """读取单用例的完整轨迹与全部断言结果，供回放查看。"""
    run = await session.get(Run, run_id)
    if run is None:
        return None
    row = (
        await session.execute(
            select(CaseResult, Case)
            .join(Case, CaseResult.case_id == Case.id)
            .where(CaseResult.run_id == run_id, Case.case_key == case_key)
        )
    ).first()
    if row is None:
        return None
    result, case = row
    assertions = (
        await session.scalars(
            select(AssertionResult)
            .where(AssertionResult.case_result_id == result.id)
            .order_by(AssertionResult.seq)
        )
    ).all()
    return {
        "run_id": run_id,
        "case_key": case.case_key,
        "case_name": case.name,
        "status": result.status,
        "error": result.error,
        "latency_ms": result.latency_ms,
        "cost_usd": result.cost_usd,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "trajectory": result.trajectory,
        "assertions": [
            {
                "seq": item.seq,
                "type": item.type,
                "passed": item.passed,
                "score": item.score,
                "detail": item.detail,
            }
            for item in assertions
        ],
    }
