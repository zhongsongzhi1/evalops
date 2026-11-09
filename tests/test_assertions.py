"""断言引擎测试。"""

from __future__ import annotations

from evalops.adapters.base import Step, Trajectory
from evalops.assertions import evaluate_assertions
from evalops.cases.dsl import CaseSpec


def _case(assertions: list[dict]) -> CaseSpec:
    return CaseSpec.model_validate(
        {"name": "断言用例", "key": "assert_case", "input": {}, "assertions": assertions}
    )


def _trajectory(output: str = "", calls: list[Step] | None = None, latency_ms: int = 100):
    return Trajectory(output=output, steps=calls or [], latency_ms=latency_ms)


def test_contains_and_not_contains() -> None:
    case = _case(
        [
            {"type": "contains", "values": ["退款", "7天"]},
            {"type": "not_contains", "values": ["保证退款"]},
        ]
    )
    outcomes = evaluate_assertions(case, _trajectory("超过7天不支持无理由退款"))
    assert [item.passed for item in outcomes] == [True, True]

    failed = evaluate_assertions(case, _trajectory("保证退款，尽快到账"))
    assert failed[0].passed is False
    assert failed[1].passed is False
    assert failed[1].detail["hits"] == ["保证退款"]


def test_regex_and_equals() -> None:
    case = _case(
        [
            {"type": "regex", "pattern": r"订单\s*A1B2C3"},
            {"type": "equals", "value": "订单 A1B2C3 已发货"},
        ]
    )
    outcomes = evaluate_assertions(case, _trajectory("订单 A1B2C3 已发货"))
    assert all(item.passed for item in outcomes)


def test_tool_calls_wildcard_and_must_not_call() -> None:
    query = Step(step=1, type="tool", name="query_order", input={"order_id": "X9"})
    case = _case(
        [
            {
                "type": "tool_calls",
                "tools": [{"name": "query_order", "args_contains": {"order_id": "*"}}],
            },
            {"type": "tool_calls", "tools": [{"name": "refund_now", "must_call": False}]},
        ]
    )
    outcomes = evaluate_assertions(case, _trajectory("ok", [query]))
    assert all(item.passed for item in outcomes)

    bad = _case(
        [
            {
                "type": "tool_calls",
                "tools": [{"name": "query_order", "args_contains": {"order_id": "A1"}}],
            }
        ]
    )
    failed = evaluate_assertions(bad, _trajectory("ok", [query]))
    assert failed[0].passed is False
    assert "参数都不满足期望" in failed[0].detail["violations"][0]

    forbidden = _case(
        [{"type": "tool_calls", "tools": [{"name": "query_order", "must_call": False}]}]
    )
    violated = evaluate_assertions(forbidden, _trajectory("ok", [query]))
    assert violated[0].passed is False


def test_latency_and_cost() -> None:
    case = _case(
        [
            {"type": "max_latency_ms", "value": 500},
            {"type": "max_cost_usd", "value": 0.001},
        ]
    )
    outcomes = evaluate_assertions(case, _trajectory("ok", latency_ms=1200), cost_usd=0.002)
    assert [item.passed for item in outcomes] == [False, False]
