"""Judge 校准：Cohen's κ、标注导入与门禁 require_kappa。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalops.calibration import CalibrationError, cohen_kappa
from evalops.cli import app
from evalops.regression import GateRules, GateSpec, RegressionReport, apply_gate

runner = CliRunner()


def test_perfect_agreement() -> None:
    result = cohen_kappa([1, 2, 3, 4, 5], [1, 2, 3, 4, 5])
    assert result.kappa == 1.0
    assert result.kappa_weighted == 1.0
    assert result.n == 5
    assert result.sufficient is False


def test_chance_level_agreement() -> None:
    result = cohen_kappa([1, 1, 2, 2], [1, 2, 1, 2])
    assert result.kappa == pytest.approx(0.0)
    assert result.kappa_weighted == pytest.approx(0.0)
    assert result.observed_agreement == pytest.approx(0.5)
    assert result.expected_agreement == pytest.approx(0.5)


def test_kappa_rejects_invalid_scores() -> None:
    with pytest.raises(CalibrationError):
        cohen_kappa([0, 1], [1, 2])


def _report(**overrides: object) -> RegressionReport:
    values: dict[str, object] = {
        "baseline_run_id": "base",
        "candidate_run_id": "cand",
        "project_id": "p1",
        "baseline_pass_rate": 1.0,
        "candidate_pass_rate": 1.0,
        "delta": 0.0,
        "baseline_assertion_pass_rate": 1.0,
        "candidate_assertion_pass_rate": 1.0,
        "cost_delta_ratio": 0.0,
    }
    values.update(overrides)
    return RegressionReport(**values)  # type: ignore[arg-type]


def _gate(threshold: float) -> GateSpec:
    return GateSpec(rules=GateRules(require_kappa=threshold))


def test_gate_blocks_without_calibration() -> None:
    report = apply_gate(_report(), _gate(0.6))
    assert report.verdict == "block"
    assert any("尚无人工标注" in reason for reason in report.reasons)


def test_gate_blocks_when_kappa_below_threshold() -> None:
    report = apply_gate(_report(judge_kappa=0.42, judge_kappa_n=60), _gate(0.6))
    assert report.verdict == "block"
    assert any("低于门禁要求" in reason for reason in report.reasons)


def test_gate_passes_with_kappa_and_notes_small_sample() -> None:
    report = apply_gate(_report(judge_kappa=0.75, judge_kappa_n=10), _gate(0.6))
    assert report.verdict == "pass"
    assert any("样本仅 10 条" in note for note in report.notes)


def _run(workdir: Path, *extra: str):
    return runner.invoke(
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
            *extra,
        ],
    )


def test_label_import_kappa_and_gate(workdir: Path) -> None:
    baseline = _run(workdir, "--baseline")
    assert baseline.exit_code == 0, baseline.output
    baseline_id = json.loads(baseline.output)["run_id"]

    gate_file = workdir / "gates" / "kappa.yaml"
    gate_file.write_text(
        "name: kappa-gate\nrules:\n  require_kappa: 0.6\n", encoding="utf-8"
    )

    candidate = _run(workdir)
    assert candidate.exit_code == 0, candidate.output
    candidate_id = json.loads(candidate.output)["run_id"]

    blocked = runner.invoke(
        app,
        ["gate", "--candidate", candidate_id, "--gate", str(gate_file), "--json"],
    )
    assert blocked.exit_code == 2, blocked.output
    blocked_report = json.loads(blocked.output)
    assert blocked_report["verdict"] == "block"
    assert any("尚无人工标注" in reason for reason in blocked_report["reasons"])

    labels = workdir / "labels.csv"
    labels.write_text(
        "case_key,human_score,notes\njudge_refund_quality,4,结论一致\n",
        encoding="utf-8",
    )
    imported = runner.invoke(
        app, ["label", "--run", baseline_id, "--csv", str(labels), "--json"]
    )
    assert imported.exit_code == 0, imported.output
    imported_payload = json.loads(imported.output)
    assert imported_payload["imported"] == 1
    assert imported_payload["skipped"] == 0

    kappa = runner.invoke(
        app, ["kappa", "--project", "demo-agent", "--threshold", "0.6", "--json"]
    )
    assert kappa.exit_code == 0, kappa.output
    kappa_payload = json.loads(kappa.output)
    assert kappa_payload["kappa"] == 1.0
    assert kappa_payload["n"] == 1
    assert kappa_payload["sufficient"] is False

    allowed = runner.invoke(
        app,
        ["gate", "--candidate", candidate_id, "--gate", str(gate_file), "--json"],
    )
    assert allowed.exit_code == 0, allowed.output
    allowed_report = json.loads(allowed.output)
    assert allowed_report["judge_kappa"] == 1.0
    assert allowed_report["judge_kappa_n"] == 1
    assert allowed_report["verdict"] == "pass"


def test_duplicate_label_import_updates_instead_of_inserting(workdir: Path) -> None:
    baseline = _run(workdir, "--baseline")
    baseline_id = json.loads(baseline.output)["run_id"]
    labels = workdir / "labels.csv"
    labels.write_text(
        "case_key,human_score,notes\njudge_refund_quality,3,初评\n", encoding="utf-8"
    )
    first = runner.invoke(app, ["label", "--run", baseline_id, "--csv", str(labels), "--json"])
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["imported"] == 1

    labels.write_text(
        "case_key,human_score,notes\njudge_refund_quality,5,复核\n", encoding="utf-8"
    )
    second = runner.invoke(app, ["label", "--run", baseline_id, "--csv", str(labels), "--json"])
    assert second.exit_code == 0, second.output
    payload = json.loads(second.output)
    assert payload["imported"] == 0
    assert payload["updated"] == 1


def test_kappa_without_labels_fails(workdir: Path) -> None:
    _run(workdir, "--baseline")
    result = runner.invoke(app, ["kappa", "--project", "demo-agent", "--json"])
    assert result.exit_code == 1
