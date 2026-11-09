"""门禁回归测试（阶段 0 验收项）。

改坏一条 golden 用例的录制输出后：
1. 候选运行出现失败用例；
2. gate 返回 block（退出码 2）；
3. 退化用例通过 case_key 被精确定位。
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from evalops.cli import app

runner = CliRunner()


def _run(workdir: Path, *extra: str):
    return runner.invoke(
        app,
        [
            "run",
            "--project",
            str(workdir / "projects" / "fake.yaml"),
            "--cases",
            str(workdir / "cases"),
            "--json",
            *extra,
        ],
    )


def test_broken_golden_case_blocks_gate(workdir: Path) -> None:
    baseline = _run(workdir, "--baseline")
    assert baseline.exit_code == 0, baseline.output
    baseline_payload = json.loads(baseline.output)
    assert baseline_payload["passed"] == 3

    fixture_path = workdir / "fixtures" / "refund_policy.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["output"] = "您的课程可以退款，退款期 30 天，保证退款。"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")

    candidate = _run(workdir)
    assert candidate.exit_code == 0, candidate.output
    candidate_payload = json.loads(candidate.output)
    assert candidate_payload["passed"] == 2
    assert candidate_payload["failed"] == 1

    gate = runner.invoke(
        app,
        [
            "gate",
            "--candidate",
            candidate_payload["run_id"],
            "--gate",
            str(workdir / "gates" / "default.yaml"),
            "--json",
        ],
    )
    assert gate.exit_code == 2, gate.output
    report = json.loads(gate.output)
    assert report["verdict"] == "block"
    assert report["new_failures"] == ["refund_policy"]
    assert report["delta"] < 0


def test_alignment_across_case_sets_uses_case_key(workdir: Path) -> None:
    baseline = _run(workdir, "--baseline")
    assert baseline.exit_code == 0, baseline.output

    # 改动用例内容 → content_hash 变化 → 生成新 case_set 与新 case_id
    case_file = workdir / "cases" / "order_lookup.yaml"
    updated = case_file.read_text(encoding="utf-8").replace(
        "tags: [regression]", "tags: [regression, nightly]"
    )
    case_file.write_text(updated, encoding="utf-8")
    candidate = _run(workdir)
    assert candidate.exit_code == 0, candidate.output
    candidate_payload = json.loads(candidate.output)

    compare = runner.invoke(
        app, ["compare", "--candidate", candidate_payload["run_id"], "--json"]
    )
    assert compare.exit_code == 0, compare.output
    report = json.loads(compare.output)
    assert report["new_failures"] == []
    assert report["added"] == []
    assert report["removed"] == []
    assert report["baseline_run_id"] == json.loads(baseline.output)["run_id"]
