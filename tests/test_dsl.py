"""DSL 校验规则测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from evalops.cases.dsl import CaseSpec, DslError, load_cases, load_project


def test_golden_cases_load(workdir: Path) -> None:
    loaded = load_cases(workdir / "cases")
    assert sorted(case.case_key for case in loaded.cases) == [
        "judge_refund_quality",
        "order_lookup",
        "out_of_scope",
        "refund_policy",
    ]
    assert len(loaded.content_hash) == 64


def test_case_key_defaults_to_name() -> None:
    case = CaseSpec.model_validate(
        {
            "name": "退款政策",
            "input": {"messages": []},
            "assertions": [{"type": "contains", "values": ["退款"]}],
        }
    )
    assert case.case_key == "退款政策"


def test_explicit_case_key_must_be_safe() -> None:
    with pytest.raises(ValueError, match="key 只能包含"):
        CaseSpec.model_validate(
            {
                "name": "退款政策",
                "key": "退款/政策",
                "input": {},
                "assertions": [{"type": "contains", "values": ["退款"]}],
            }
        )


def test_duplicate_case_key_rejected(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(
        "name: A\nkey: same\ninput: {}\nassertions:\n  - type: contains\n    values: [A]\n",
        encoding="utf-8",
    )
    (tmp_path / "b.yaml").write_text(
        "name: B\nkey: same\ninput: {}\nassertions:\n  - type: contains\n    values: [B]\n",
        encoding="utf-8",
    )
    with pytest.raises(DslError, match="case_key 重复"):
        load_cases(tmp_path)


def test_case_requires_assertion(tmp_path: Path) -> None:
    case_file = tmp_path / "empty.yaml"
    case_file.write_text("name: 空断言\ninput: {}\n", encoding="utf-8")
    with pytest.raises(DslError, match="至少包含一条断言"):
        load_cases(case_file)


def test_unknown_assertion_type_rejected(tmp_path: Path) -> None:
    case_file = tmp_path / "bad.yaml"
    case_file.write_text(
        "name: 未知断言\ninput: {}\nassertions:\n  - type: magic\n    value: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(DslError):
        load_cases(case_file)


def test_project_adapter_whitelist(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text("name: demo\nadapter: shell\n", encoding="utf-8")
    with pytest.raises(DslError):
        load_project(project_file)


def test_langgraph_requires_entrypoint(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text("name: demo\nadapter: langgraph\n", encoding="utf-8")
    with pytest.raises(DslError, match="entrypoint"):
        load_project(project_file)


def test_http_requires_endpoint(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text("name: demo\nadapter: http\n", encoding="utf-8")
    with pytest.raises(DslError, match="endpoint"):
        load_project(project_file)

    project_file.write_text(
        "name: demo\nadapter: http\nconfig:\n  endpoint: ftp://example.com\n",
        encoding="utf-8",
    )
    with pytest.raises(DslError, match="endpoint"):
        load_project(project_file)


def test_expected_shorthand_compiles_first() -> None:
    case = CaseSpec.model_validate(
        {
            "name": "简写",
            "input": {},
            "expected": {
                "answer_contains": ["A"],
                "tool_calls": [{"name": "query_order"}],
            },
            "assertions": [{"type": "max_latency_ms", "value": 1000}],
        }
    )
    types = [item.type for item in case.compiled_assertions()]
    assert types == ["contains", "tool_calls", "max_latency_ms"]
