"""回归对比与门禁判定。

对齐规则（设计文档 §19.2）：
- 按 ``case_key`` 对齐 baseline 与 candidate，而不是 ``case_id``；
- 只在候选出现的用例记为「新增」，不计入 New Failures；
- 只在基线出现的用例记为「移除」，可由门禁配置是否允许。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from evalops.calibration import MIN_CALIBRATION_SAMPLES, compute_project_kappa
from evalops.db.models import AssertionResult, Baseline, Case, CaseResult, Regression, Run
from evalops.db.session import Database


class RegressionError(ValueError):
    """回归对比或门禁配置错误。"""


class GateRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_pass_rate_drop: float = Field(default=0.02, ge=0.0, le=1.0)
    max_new_failures: int | None = Field(default=0, ge=0)
    max_cost_increase: float | None = Field(default=None, ge=0.0)
    require_kappa: float | None = Field(default=None, ge=0.0, le=1.0)
    allow_removed_cases: bool = True


class GateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "default"
    scope_tags: list[str] = Field(default_factory=list)
    rules: GateRules = Field(default_factory=GateRules)


def load_gate(path: str | Path | None) -> GateSpec:
    if path is None:
        return GateSpec()
    file_path = Path(path)
    if not file_path.is_file():
        raise RegressionError(f"门禁文件不存在: {file_path}")
    try:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegressionError(f"{file_path}: YAML 解析失败: {exc}") from exc
    try:
        return GateSpec.model_validate(data)
    except ValidationError as exc:
        raise RegressionError(f"{file_path}: 门禁配置校验失败\n{exc}") from exc


@dataclass
class CaseDelta:
    case_key: str
    baseline: str | None
    candidate: str | None
    change: str


@dataclass
class RegressionReport:
    baseline_run_id: str
    candidate_run_id: str
    project_id: str
    baseline_pass_rate: float
    candidate_pass_rate: float
    delta: float
    baseline_assertion_pass_rate: float
    candidate_assertion_pass_rate: float
    cost_delta_ratio: float
    judge_kappa: float | None = None
    judge_kappa_weighted: float | None = None
    judge_kappa_n: int = 0
    new_failures: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    cases: list[CaseDelta] = field(default_factory=list)
    verdict: str = "pass"
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _get_run(session: Any, run_id: str) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise RegressionError(f"运行不存在: {run_id}")
    return run


async def _get_run_by_ref(session: Any, ref: str) -> Run:
    run = await session.get(Run, ref)
    if run is not None:
        return run
    if len(ref) >= 4:
        rows = (await session.scalars(select(Run).where(Run.id.startswith(ref)))).all()
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise RegressionError(f"运行 ID 前缀不唯一: {ref}")
    raise RegressionError(f"找不到运行: {ref}")


async def resolve_runs(
    session: Any, baseline_ref: str = "baseline", candidate_ref: str = "latest"
) -> tuple[Run, Run]:
    """解析运行引用。

    ``baseline`` 表示该项目最近一次被标记的基线，``latest`` 表示最近一次运行；
    其他值按完整 ID 或唯一前缀匹配。
    """
    base = (
        None
        if baseline_ref in ("baseline", "latest")
        else await _get_run_by_ref(session, baseline_ref)
    )
    cand = (
        None
        if candidate_ref in ("baseline", "latest")
        else await _get_run_by_ref(session, candidate_ref)
    )

    anchor = base if base is not None else cand
    project_id = anchor.project_id if anchor is not None else None
    if project_id is None:
        latest_mark = await session.scalar(
            select(Baseline).order_by(Baseline.created_at.desc(), Baseline.id.desc())
        )
        if latest_mark is None:
            raise RegressionError("还没有基线运行；请先执行 `evalops run ... --baseline`")
        project_id = latest_mark.project_id

    if base is None:
        mark = await session.scalar(
            select(Baseline)
            .where(Baseline.project_id == project_id)
            .order_by(Baseline.created_at.desc(), Baseline.id.desc())
        )
        if mark is None:
            raise RegressionError("该项目还没有基线运行；请先执行 `evalops run ... --baseline`")
        base = await _get_run(session, mark.run_id)

    if cand is None:
        if candidate_ref == "baseline":
            cand = base
        else:
            cand = await session.scalar(
                select(Run)
                .where(Run.project_id == project_id)
                .order_by(Run.started_at.desc(), Run.id.desc())
            )
            if cand is None:
                raise RegressionError("该项目还没有候选运行")

    if base.project_id != cand.project_id:
        raise RegressionError("基线与候选不属于同一个项目，不能对比 Pass Rate")
    return base, cand


async def _case_states(session: Any, run_id: str) -> dict[str, str]:
    rows = (
        await session.execute(
            select(Case.case_key, CaseResult.status)
            .join(CaseResult, CaseResult.case_id == Case.id)
            .where(CaseResult.run_id == run_id)
        )
    ).all()
    return {case_key: status for case_key, status in rows}


async def _assertion_pass_rate(session: Any, run_id: str) -> float:
    base_stmt = (
        select(func.count())
        .select_from(AssertionResult)
        .join(CaseResult, AssertionResult.case_result_id == CaseResult.id)
        .where(CaseResult.run_id == run_id)
    )
    total = int(await session.scalar(base_stmt) or 0)
    if total == 0:
        return 0.0
    passed = int(await session.scalar(base_stmt.where(AssertionResult.passed.is_(True))) or 0)
    return passed / total


def _pass_rate(states: dict[str, str]) -> float:
    if not states:
        return 0.0
    return sum(1 for status in states.values() if status == "passed") / len(states)


async def build_report(
    session: Any, baseline_run_id: str, candidate_run_id: str
) -> RegressionReport:
    baseline = await _get_run(session, baseline_run_id)
    candidate = await _get_run(session, candidate_run_id)
    if baseline.project_id != candidate.project_id:
        raise RegressionError("基线与候选不属于同一个项目，不能对比 Pass Rate")

    baseline_states = await _case_states(session, baseline.id)
    candidate_states = await _case_states(session, candidate.id)
    shared = sorted(set(baseline_states) & set(candidate_states))

    new_failures = [
        key
        for key in shared
        if baseline_states[key] == "passed" and candidate_states[key] != "passed"
    ]
    fixed = [
        key
        for key in shared
        if baseline_states[key] != "passed" and candidate_states[key] == "passed"
    ]
    added = sorted(set(candidate_states) - set(baseline_states))
    removed = sorted(set(baseline_states) - set(candidate_states))

    diffs = []
    for key in sorted(set(baseline_states) | set(candidate_states)):
        before = baseline_states.get(key)
        after = candidate_states.get(key)
        if before is None:
            change = "added"
        elif after is None:
            change = "removed"
        elif before == after:
            change = "same"
        elif before == "passed":
            change = "new_failure"
        elif after == "passed":
            change = "fixed"
        else:
            change = "still_failing"
        diffs.append(CaseDelta(case_key=key, baseline=before, candidate=after, change=change))

    baseline_pass_rate = _pass_rate(baseline_states)
    candidate_pass_rate = _pass_rate(candidate_states)
    if baseline.cost_usd > 0:
        cost_delta_ratio = (candidate.cost_usd - baseline.cost_usd) / baseline.cost_usd
    else:
        cost_delta_ratio = 0.0

    return RegressionReport(
        baseline_run_id=baseline.id,
        candidate_run_id=candidate.id,
        project_id=baseline.project_id,
        baseline_pass_rate=baseline_pass_rate,
        candidate_pass_rate=candidate_pass_rate,
        delta=candidate_pass_rate - baseline_pass_rate,
        baseline_assertion_pass_rate=await _assertion_pass_rate(session, baseline.id),
        candidate_assertion_pass_rate=await _assertion_pass_rate(session, candidate.id),
        cost_delta_ratio=cost_delta_ratio,
        new_failures=sorted(new_failures),
        fixed=sorted(fixed),
        added=added,
        removed=removed,
        cases=diffs,
    )


def apply_gate(report: RegressionReport, gate: GateSpec) -> RegressionReport:
    """按门禁规则给出 verdict：pass / block / warn。"""
    rules = gate.rules
    reasons: list[str] = []
    notes: list[str] = []
    blocked = False

    if rules.max_new_failures is not None and len(report.new_failures) > rules.max_new_failures:
        blocked = True
        reasons.append(
            f"New Failures {len(report.new_failures)} 条，超过允许的 {rules.max_new_failures} 条"
        )

    drop = report.baseline_pass_rate - report.candidate_pass_rate
    if drop > rules.max_pass_rate_drop + 1e-9:
        blocked = True
        reasons.append(
            f"通过率跌幅 {drop:.1%}，超过允许的 {rules.max_pass_rate_drop:.1%}"
        )

    if (
        rules.max_cost_increase is not None
        and report.cost_delta_ratio > rules.max_cost_increase + 1e-9
    ):
        blocked = True
        reasons.append(
            f"成本上涨 {report.cost_delta_ratio:.1%}，超过允许的 {rules.max_cost_increase:.1%}"
        )

    if not rules.allow_removed_cases and report.removed:
        blocked = True
        reasons.append(f"存在被移除的用例: {', '.join(report.removed)}")

    if rules.require_kappa is not None:
        threshold = rules.require_kappa
        if report.judge_kappa is None:
            blocked = True
            reasons.append(
                f"门禁要求 Judge κ ≥ {threshold:.2f}，但项目尚无人工标注"
                "（先执行 evalops label 与 evalops kappa）"
            )
        elif report.judge_kappa < threshold:
            blocked = True
            reasons.append(
                f"Judge κ {report.judge_kappa:.2f} 低于门禁要求的 {threshold:.2f}"
            )
        elif report.judge_kappa_n < MIN_CALIBRATION_SAMPLES:
            notes.append(
                f"Judge κ {report.judge_kappa:.2f} 达标，但样本仅 "
                f"{report.judge_kappa_n} 条（建议 ≥ {MIN_CALIBRATION_SAMPLES}）"
            )
        else:
            notes.append(f"Judge κ {report.judge_kappa:.2f}（n={report.judge_kappa_n}）达标")

    if blocked:
        report.verdict = "block"
        report.reasons = reasons
        report.notes = notes
        return report

    warnings: list[str] = []
    if report.new_failures:
        warnings.append(
            f"存在 {len(report.new_failures)} 条 New Failure，但在阈值内"
        )
    if drop > 0:
        warnings.append(f"通过率下降 {drop:.1%}，但在阈值内")
    if report.removed and rules.allow_removed_cases:
        warnings.append(f"移除用例 {len(report.removed)} 条: {', '.join(report.removed)}")
    if report.added:
        warnings.append(f"新增用例 {len(report.added)} 条，不计入 New Failures")

    report.verdict = "warn" if warnings else "pass"
    report.reasons = warnings or ["New Failures、通过率跌幅与成本均在阈值内"]
    report.notes = notes
    return report


async def save_report(session: Any, report: RegressionReport) -> None:
    session.add(
        Regression(
            baseline_run_id=report.baseline_run_id,
            candidate_run_id=report.candidate_run_id,
            summary=report.to_dict(),
            verdict=report.verdict,
        )
    )
    await session.commit()


async def compare_runs(
    db: Database,
    baseline_ref: str = "baseline",
    candidate_ref: str = "latest",
    gate: GateSpec | None = None,
) -> RegressionReport:
    async with db.sessionmaker() as session:
        baseline, candidate = await resolve_runs(session, baseline_ref, candidate_ref)
        report = await build_report(session, baseline.id, candidate.id)
        kappa = await compute_project_kappa(session, report.project_id)
        if kappa is not None:
            report.judge_kappa = kappa.kappa
            report.judge_kappa_weighted = kappa.kappa_weighted
            report.judge_kappa_n = kappa.n
        report = apply_gate(report, gate or GateSpec())
        await save_report(session, report)
        return report
