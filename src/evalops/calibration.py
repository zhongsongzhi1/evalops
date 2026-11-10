"""Judge 校准：人工标注导入与 Cohen's κ 计算。

设计文档 §7、§19.3：
- 标注必须绑定到具体某次输出（``case_result_id``），而不是用例；
- κ ≥ 0.6 才允许 Judge 进入门禁；
- 样本量建议 ≥ 50 条，不足时给出提示但不伪造结论。
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from evalops.db.models import AssertionResult, Case, CaseResult, CaseSet, JudgeLabel, Run

MIN_CALIBRATION_SAMPLES = 50
SCORE_MIN = 1
SCORE_MAX = 5


class CalibrationError(ValueError):
    """标注数据或 κ 计算失败。"""


@dataclass
class KappaResult:
    kappa: float
    kappa_weighted: float
    observed_agreement: float
    expected_agreement: float
    n: int
    categories: list[int]
    sufficient: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LabelImportResult:
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def cohen_kappa(human: list[int], judge: list[int]) -> KappaResult:
    """计算未加权与二次加权 Cohen's κ。"""
    if len(human) != len(judge):
        raise CalibrationError("人工评分与 Judge 评分数量不一致")
    if not human:
        raise CalibrationError("至少需要一条标注才能计算 κ")
    for value in [*human, *judge]:
        if not isinstance(value, int) or not SCORE_MIN <= value <= SCORE_MAX:
            raise CalibrationError(f"评分必须是 {SCORE_MIN}-{SCORE_MAX} 的整数，实际为 {value!r}")

    categories = sorted(set(human) | set(judge))
    positions = {value: index for index, value in enumerate(categories)}
    size = len(categories)
    observed = [[0.0] * size for _ in range(size)]
    for left, right in zip(human, judge, strict=True):
        observed[positions[left]][positions[right]] += 1.0

    total = float(len(human))
    row_totals = [sum(row) for row in observed]
    col_totals = [sum(observed[row][col] for row in range(size)) for col in range(size)]
    observed_agreement = sum(observed[index][index] for index in range(size)) / total
    expected_agreement = sum(
        row_totals[index] * col_totals[index] for index in range(size)
    ) / (total * total)

    if expected_agreement >= 1.0 - 1e-12:
        kappa = 1.0 if observed_agreement >= 1.0 - 1e-12 else 0.0
    else:
        kappa = (observed_agreement - expected_agreement) / (1.0 - expected_agreement)

    span = max(1, categories[-1] - categories[0])
    weighted_observed = 0.0
    weighted_expected = 0.0
    for row_index, row_value in enumerate(categories):
        for col_index, col_value in enumerate(categories):
            weight = ((row_value - col_value) / span) ** 2
            weighted_observed += weight * observed[row_index][col_index]
            weighted_expected += weight * row_totals[row_index] * col_totals[col_index] / total
    weighted = (
        1.0 - weighted_observed / weighted_expected
        if weighted_expected > 1e-12
        else kappa
    )

    return KappaResult(
        kappa=round(max(-1.0, min(1.0, kappa)), 4),
        kappa_weighted=round(max(-1.0, min(1.0, weighted)), 4),
        observed_agreement=round(observed_agreement, 4),
        expected_agreement=round(expected_agreement, 4),
        n=len(human),
        categories=categories,
        sufficient=len(human) >= MIN_CALIBRATION_SAMPLES,
    )


async def compute_project_kappa(
    session: Any, project_id: str | None = None, rubric_version: str | None = None
) -> KappaResult | None:
    """汇总 ``judge_label`` 计算 κ；没有标注时返回 ``None``。"""
    stmt = (
        select(JudgeLabel.human_score, JudgeLabel.judge_score)
        .join(Case, JudgeLabel.case_id == Case.id)
        .join(CaseSet, Case.case_set_id == CaseSet.id)
    )
    if project_id is not None:
        stmt = stmt.where(CaseSet.project_id == project_id)
    if rubric_version is not None:
        stmt = stmt.where(JudgeLabel.rubric_version == rubric_version)
    rows = (await session.execute(stmt)).all()
    if not rows:
        return None
    return cohen_kappa(
        [int(row[0]) for row in rows],
        [int(row[1]) for row in rows],
    )


async def resolve_run_ref(session: Any, reference: str) -> Run:
    """支持完整 ID、唯一前缀与 ``latest``。"""
    run = await session.get(Run, reference)
    if run is not None:
        return run
    if reference == "latest":
        latest = await session.scalar(
            select(Run).order_by(Run.started_at.desc(), Run.id.desc())
        )
        if latest is None:
            raise CalibrationError("还没有任何运行，请先执行 evalops run")
        return latest
    if len(reference) >= 4:
        rows = (await session.scalars(select(Run).where(Run.id.startswith(reference)))).all()
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise CalibrationError(f"运行 ID 前缀不唯一: {reference}")
    raise CalibrationError(f"找不到运行: {reference}")


async def import_labels(
    session: Any,
    run_id: str,
    csv_path: str | Path,
    labeled_by: str = "human",
) -> LabelImportResult:
    """从 CSV 导入人工评分：``case_key,human_score[,notes]``。"""
    path = Path(csv_path)
    if not path.is_file():
        raise CalibrationError(f"标注文件不存在: {path}")
    rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    if not rows:
        raise CalibrationError(f"{path}: 标注文件为空")

    run = await session.get(Run, run_id)
    if run is None:
        raise CalibrationError(f"运行不存在: {run_id}")

    case_rows = (
        await session.execute(
            select(CaseResult, Case)
            .join(Case, CaseResult.case_id == Case.id)
            .where(CaseResult.run_id == run_id)
        )
    ).all()
    results_by_key = {case.case_key: (result, case) for result, case in case_rows}
    result_ids = [result.id for result, _ in case_rows]

    judge_scores: dict[str, tuple[float, str]] = {}
    if result_ids:
        assertions = (
            await session.scalars(
                select(AssertionResult).where(
                    AssertionResult.case_result_id.in_(result_ids),
                    AssertionResult.type == "llm_judge",
                )
            )
        ).all()
        for assertion in assertions:
            if assertion.score is None:
                continue
            version = str((assertion.detail or {}).get("rubric_version") or "unknown")
            judge_scores.setdefault(assertion.case_result_id, (float(assertion.score), version))

    existing: dict[tuple[str, str, str], JudgeLabel] = {}
    if result_ids:
        labels = (
            await session.scalars(
                select(JudgeLabel).where(JudgeLabel.case_result_id.in_(result_ids))
            )
        ).all()
        existing = {
            (label.case_result_id, label.rubric_version, label.labeled_by): label
            for label in labels
        }

    outcome = LabelImportResult()
    for line_no, row in enumerate(rows, start=2):
        case_key = (row.get("case_key") or "").strip()
        raw_score = (row.get("human_score") or "").strip()
        if case_key not in results_by_key:
            outcome.skipped += 1
            outcome.errors.append(f"第 {line_no} 行: 本次运行中没有用例 {case_key!r}")
            continue
        try:
            human_score = int(raw_score)
        except ValueError:
            human_score = 0
        if not SCORE_MIN <= human_score <= SCORE_MAX:
            outcome.skipped += 1
            outcome.errors.append(
                f"第 {line_no} 行: human_score 必须是 {SCORE_MIN}-{SCORE_MAX} 的整数"
            )
            continue

        result, case = results_by_key[case_key]
        judged = judge_scores.get(result.id)
        if judged is None:
            outcome.skipped += 1
            outcome.errors.append(
                f"第 {line_no} 行: 用例 {case_key} 没有 llm_judge 结果，无法校准"
            )
            continue

        judge_score, rubric_version = judged
        notes = (row.get("notes") or "").strip() or None
        key = (result.id, rubric_version, labeled_by)
        if key in existing:
            label = existing[key]
            label.human_score = human_score
            label.judge_score = round(judge_score)
            label.notes = notes
            outcome.updated += 1
        else:
            session.add(
                JudgeLabel(
                    case_id=case.id,
                    case_result_id=result.id,
                    human_score=human_score,
                    judge_score=round(judge_score),
                    rubric_version=rubric_version,
                    labeled_by=labeled_by,
                    notes=notes,
                )
            )
            outcome.imported += 1

    await session.commit()
    return outcome
