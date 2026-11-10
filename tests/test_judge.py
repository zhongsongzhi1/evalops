"""llm_judge 断言、rubric 版本与 Judge 构建测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalops.adapters.base import Trajectory
from evalops.assertions import evaluate_assertions
from evalops.cases.dsl import CaseSpec
from evalops.cli import app
from evalops.judge import JudgeError, MockJudge, build_judge, load_rubric

runner = CliRunner()

RUBRIC = """---
version: refund_v1
reference: "超过7天不支持无理由退款，需要人工审核"
---

# 评分标准

- 5：结论准确
- 1：承诺无条件退款
"""


def _write_rubric(tmp_path: Path, *, text: str = RUBRIC) -> Path:
    path = tmp_path / "rubrics" / "refund.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _case(rubric: str, pass_score: float = 4.0, samples: int = 3) -> CaseSpec:
    return CaseSpec.model_validate(
        {
            "name": "退款评审",
            "key": "judge_case",
            "input": {},
            "assertions": [
                {
                    "type": "llm_judge",
                    "rubric": rubric,
                    "pass_score": pass_score,
                    "samples": samples,
                }
            ],
        }
    )


def test_rubric_front_matter_is_parsed(tmp_path: Path) -> None:
    path = _write_rubric(tmp_path)
    rubric = load_rubric("rubrics/refund.md", [tmp_path])
    assert rubric.path == path
    assert rubric.version == "refund_v1"
    assert rubric.reference == "超过7天不支持无理由退款，需要人工审核"


def test_rubric_version_defaults_to_content_hash(tmp_path: Path) -> None:
    path = tmp_path / "plain.md"
    path.write_text("# 没有 front matter", encoding="utf-8")
    rubric = load_rubric("plain.md", [tmp_path])
    assert len(rubric.version) == 12
    assert rubric.reference is None


def test_missing_rubric_lists_searched_paths(tmp_path: Path) -> None:
    with pytest.raises(JudgeError, match="rubric 文件不存在"):
        load_rubric("missing.md", [tmp_path])


async def test_llm_judge_pass_and_fail(tmp_path: Path) -> None:
    _write_rubric(tmp_path)
    case = _case("rubrics/refund.md")

    passed = await evaluate_assertions(
        case,
        Trajectory(output="超过7天不支持无理由退款，需要人工审核"),
        rubric_dirs=[tmp_path],
    )
    assert passed[0].passed is True
    assert passed[0].detail["rubric_version"] == "refund_v1"
    assert passed[0].detail["provider"] == "mock"
    assert len(passed[0].detail["scores"]) == 3
    assert passed[0].detail["median"] >= 4

    failed = await evaluate_assertions(
        case,
        Trajectory(output="保证退款，马上到账"),
        rubric_dirs=[tmp_path],
    )
    assert failed[0].passed is False
    assert failed[0].detail["median"] < 4


async def test_mock_judge_requires_reference(tmp_path: Path) -> None:
    path = tmp_path / "no_ref.md"
    path.write_text("---\nversion: v1\n---\n# rubric", encoding="utf-8")
    rubric = load_rubric("no_ref.md", [tmp_path])
    with pytest.raises(JudgeError, match="reference"):
        await MockJudge().score(rubric, "输出", 1)


def test_build_judge_defaults_to_mock() -> None:
    assert isinstance(build_judge({}), MockJudge)


def test_openai_judge_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(JudgeError, match="OPENAI_API_KEY"):
        build_judge({"judge": {"provider": "openai"}})


def test_unknown_judge_provider_rejected() -> None:
    with pytest.raises(JudgeError, match="未知 judge provider"):
        build_judge({"judge": {"provider": "magic"}})


def test_cli_judge_case_end_to_end(workdir: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--project",
            str(workdir / "projects" / "fake.yaml"),
            "--cases",
            str(workdir / "cases"),
            "--tag",
            "judge",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total"] == 1
    assert payload["passed"] == 1
    assert payload["assertion_pass_rate"] == 1.0

    fixture_path = workdir / "fixtures" / "judge_refund_quality.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["output"] = "保证退款，马上到账。"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")

    broken = runner.invoke(
        app,
        [
            "run",
            "--project",
            str(workdir / "projects" / "fake.yaml"),
            "--cases",
            str(workdir / "cases"),
            "--tag",
            "judge",
            "--json",
        ],
    )
    assert broken.exit_code == 0, broken.output
    broken_payload = json.loads(broken.output)
    assert broken_payload["passed"] == 0
    assert broken_payload["failed"] == 1
