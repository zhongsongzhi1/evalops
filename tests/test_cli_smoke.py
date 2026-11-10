"""CLI 端到端冒烟测试：零密钥跑通 run → compare → gate。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from evalops.cli import app

runner = CliRunner()

EXPECTED_TABLES = {
    "project",
    "case_set",
    "case",
    "run",
    "case_result",
    "assertion_result",
    "baseline",
    "regression",
    "gate",
    "judge_label",
    "tool_recording",
    "webhook_event",
}


def _args(workdir: Path, *extra: str) -> list[str]:
    return [
        *extra,
        "--project",
        str(workdir / "projects" / "fake.yaml"),
        "--cases",
        str(workdir / "cases"),
    ]


def test_run_compare_gate_pass(workdir: Path) -> None:
    result = runner.invoke(app, _args(workdir, "run", "--baseline", "--json"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total"] == 4
    assert payload["passed"] == 4
    assert payload["status"] == "succeeded"
    assert payload["pass_rate"] == 1.0
    assert payload["assertion_pass_rate"] == 1.0

    compare = runner.invoke(app, ["compare", "--candidate", payload["run_id"], "--json"])
    assert compare.exit_code == 0, compare.output
    report = json.loads(compare.output)
    assert report["new_failures"] == []
    assert report["delta"] == 0.0

    gate = runner.invoke(
        app,
        [
            "gate",
            "--candidate",
            payload["run_id"],
            "--gate",
            str(workdir / "gates" / "default.yaml"),
        ],
    )
    assert gate.exit_code == 0, gate.output
    assert "PASS" in gate.output


def test_schema_has_all_tables(workdir: Path) -> None:
    result = runner.invoke(app, _args(workdir, "run", "--json"))
    assert result.exit_code == 0, result.output

    db_path = workdir.parent / "evalops.db"
    with sqlite3.connect(db_path) as connection:
        names = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
    assert EXPECTED_TABLES <= names
    assert "alembic_version" in names


def test_run_with_missing_fixture_reports_error(workdir: Path) -> None:
    (workdir / "fixtures" / "order_lookup.json").unlink()
    result = runner.invoke(app, _args(workdir, "run", "--json"))
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["errored"] == 1
    errored = [case for case in payload["cases"] if case["status"] == "error"]
    assert errored[0]["case_key"] == "order_lookup"
    assert "缺少录制文件" in errored[0]["error"]


def test_tag_filter(workdir: Path) -> None:
    result = runner.invoke(app, _args(workdir, "run", "--tag", "refund", "--json"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total"] == 1
    assert payload["cases"][0]["case_key"] == "refund_policy"
