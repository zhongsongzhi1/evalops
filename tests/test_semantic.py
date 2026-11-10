"""semantic 断言与 embedding 来源测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalops.adapters.base import Trajectory
from evalops.assertions import evaluate_assertions
from evalops.cases.dsl import CaseSpec
from evalops.cli import app
from evalops.embeddings import EmbeddingError, MockEmbedder, build_embedder, cosine

runner = CliRunner()


def _case(reference: str, threshold: float = 0.5) -> CaseSpec:
    return CaseSpec.model_validate(
        {
            "name": "语义用例",
            "key": "semantic_case",
            "input": {},
            "assertions": [
                {"type": "semantic", "reference": reference, "threshold": threshold}
            ],
        }
    )


async def test_mock_embedding_is_deterministic() -> None:
    embedder = MockEmbedder(dim=128)
    first, second = await embedder.embed(["超过7天不支持无理由退款", "超过7天不支持无理由退款"])
    assert first == second
    assert cosine(first, second) == pytest.approx(1.0)
    assert len(first) == 128


async def test_semantic_assertion_pass_and_fail() -> None:
    passed_case = _case("超过7天不支持无理由退款", threshold=0.9)
    passed = await evaluate_assertions(
        passed_case, Trajectory(output="超过7天不支持无理由退款")
    )
    assert passed[0].passed is True
    assert passed[0].detail["provider"] == "mock"
    assert passed[0].detail["similarity"] == pytest.approx(1.0)

    failed_case = _case("超过7天不支持无理由退款", threshold=0.9)
    failed = await evaluate_assertions(failed_case, Trajectory(output="股票行情上涨"))
    assert failed[0].passed is False
    assert failed[0].detail["similarity"] < 0.9


def test_build_embedder_defaults_to_mock() -> None:
    embedder = build_embedder({})
    assert isinstance(embedder, MockEmbedder)
    hashed = build_embedder({"embedding": {"provider": "hash", "dim": 64}})
    assert isinstance(hashed, MockEmbedder)
    assert hashed.dim == 64


def test_openai_embedder_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(EmbeddingError, match="OPENAI_API_KEY"):
        build_embedder({"embedding": {"provider": "openai"}})


def test_unknown_embedding_provider_rejected() -> None:
    with pytest.raises(EmbeddingError, match="未知 embedding provider"):
        build_embedder({"embedding": {"provider": "magic"}})


def test_semantic_threshold_must_be_in_range() -> None:
    with pytest.raises(ValueError):
        _case("参考", threshold=1.5)


def test_cli_semantic_case_end_to_end(workdir: Path) -> None:
    text = "超过7天不支持无理由退款，需要人工审核"
    (workdir / "cases" / "semantic_case.yaml").write_text(
        "name: 语义用例\n"
        "key: semantic_case\n"
        "input: {}\n"
        "assertions:\n"
        "  - type: semantic\n"
        f"    reference: \"{text}\"\n"
        "    threshold: 0.99\n"
        "tags: [semantic]\n",
        encoding="utf-8",
    )
    (workdir / "fixtures" / "semantic_case.json").write_text(
        json.dumps({"output": text}, ensure_ascii=False), encoding="utf-8"
    )

    result = runner.invoke(
        app,
        [
            "run",
            "--project",
            str(workdir / "projects" / "fake.yaml"),
            "--cases",
            str(workdir / "cases"),
            "--tag",
            "semantic",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total"] == 1
    assert payload["passed"] == 1
    assert payload["assertion_pass_rate"] == 1.0
