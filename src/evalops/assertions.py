"""断言引擎：精确断言（contains / not_contains / equals / regex / tool_calls /
max_latency_ms / max_cost_usd）、semantic 语义断言与 llm_judge 评审断言。

embedding 与 Judge 来源均由项目配置显式声明（见 ``evalops.embeddings`` /
``evalops.judge``）。断言只读取轨迹，不看被测系统的内部实现。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evalops.adapters.base import Step, Trajectory
from evalops.cases.dsl import (
    CaseSpec,
    ContainsAssertion,
    EqualsAssertion,
    LlmJudgeAssertion,
    MaxCostAssertion,
    MaxLatencyAssertion,
    NotContainsAssertion,
    RegexAssertion,
    SemanticAssertion,
    ToolCallsAssertion,
)
from evalops.embeddings import Embedder, MockEmbedder, cosine
from evalops.judge import Judge, MockJudge, load_rubric


@dataclass
class AssertionOutcome:
    seq: int
    type: str
    passed: bool
    score: float
    detail: dict[str, Any]


def _arg_mismatches(actual: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key, value in expected.items():
        if key not in actual:
            problems.append(f"缺少参数 {key}")
        elif value != "*" and actual[key] != value:
            problems.append(f"{key}={actual[key]!r} 与期望 {value!r} 不一致")
    return problems


def _check_tool_calls(
    spec: ToolCallsAssertion, calls: list[Step]
) -> tuple[bool, dict[str, Any]]:
    violations: list[str] = []
    details: list[dict[str, Any]] = []

    for tool in spec.tools:
        if not tool.must_call:
            hits = [call for call in calls if call.name == tool.name]
            if hits:
                violations.append(f"{tool.name} 不应被调用，实际调用了 {len(hits)} 次")

    if spec.ordered:
        cursor = 0
        for tool in spec.tools:
            if not tool.must_call:
                continue
            matched: Step | None = None
            for index in range(cursor, len(calls)):
                if calls[index].name == tool.name:
                    matched = calls[index]
                    cursor = index + 1
                    break
            if matched is None:
                violations.append(f"未按顺序调用 {tool.name}")
                continue
            if tool.args_contains:
                problems = _arg_mismatches(matched.input, tool.args_contains)
                if problems:
                    violations.append(f"{tool.name} 参数不匹配: {'; '.join(problems)}")
    else:
        for tool in spec.tools:
            if not tool.must_call:
                continue
            matched_calls = [call for call in calls if call.name == tool.name]
            if not matched_calls:
                violations.append(f"未调用必需工具 {tool.name}")
                continue
            if tool.args_contains and not any(
                not _arg_mismatches(call.input, tool.args_contains) for call in matched_calls
            ):
                violations.append(
                    f"{tool.name} 的参数都不满足期望 {tool.args_contains!r}"
                )

    for call in calls:
        details.append({"name": call.name, "input": call.input})
    return not violations, {"calls": details, "violations": violations}


async def evaluate_assertions(
    case: CaseSpec,
    trajectory: Trajectory,
    cost_usd: float = 0.0,
    embedder: Embedder | None = None,
    judge: Judge | None = None,
    rubric_dirs: Sequence[Path] | None = None,
) -> list[AssertionOutcome]:
    """按编译后的断言顺序逐条判定，``seq`` 与 DSL 中的序号一致。"""
    output = trajectory.output
    calls = trajectory.tool_calls()
    active_embedder = embedder or MockEmbedder()
    active_judge: Judge = judge or MockJudge(embedder=active_embedder)
    search_dirs = list(rubric_dirs) if rubric_dirs else [Path.cwd()]
    outcomes: list[AssertionOutcome] = []

    for seq, spec in enumerate(case.compiled_assertions()):
        detail: dict[str, Any]
        score: float | None = None
        if isinstance(spec, ContainsAssertion):
            missing = [value for value in spec.values if value not in output]
            passed = not missing
            detail = {"missing": missing}
        elif isinstance(spec, NotContainsAssertion):
            hits = [value for value in spec.values if value in output]
            passed = not hits
            detail = {"hits": hits}
        elif isinstance(spec, EqualsAssertion):
            passed = output.strip() == spec.value.strip()
            detail = {"expected": spec.value}
        elif isinstance(spec, RegexAssertion):
            matched = re.search(spec.pattern, output) is not None
            passed = matched
            detail = {"pattern": spec.pattern, "matched": matched}
        elif isinstance(spec, ToolCallsAssertion):
            passed, detail = _check_tool_calls(spec, calls)
        elif isinstance(spec, MaxLatencyAssertion):
            passed = trajectory.latency_ms <= spec.value
            detail = {"actual_ms": trajectory.latency_ms, "max_ms": spec.value}
        elif isinstance(spec, MaxCostAssertion):
            passed = cost_usd <= spec.value
            detail = {"actual_usd": cost_usd, "max_usd": spec.value}
        elif isinstance(spec, SemanticAssertion):
            vectors = await active_embedder.embed([output, spec.reference])
            similarity = cosine(vectors[0], vectors[1])
            passed = similarity >= spec.threshold
            score = similarity
            detail = {
                "similarity": round(similarity, 4),
                "threshold": spec.threshold,
                "reference": spec.reference,
                "provider": active_embedder.name,
            }
        elif isinstance(spec, LlmJudgeAssertion):
            rubric = load_rubric(spec.rubric, search_dirs)
            judged = await active_judge.score(rubric, output, spec.samples)
            passed = judged.score >= spec.pass_score
            score = judged.score
            detail = judged.to_detail(rubric, spec.pass_score)
        else:  # pragma: no cover - 类型联合已穷尽
            raise TypeError(f"未知断言类型: {type(spec)!r}")

        outcomes.append(
            AssertionOutcome(
                seq=seq,
                type=spec.type,
                passed=passed,
                score=score if score is not None else (1.0 if passed else 0.0),
                detail=detail,
            )
        )
    return outcomes
