"""阶段 3 轨迹：逐步 diff、运行对比与单用例回放。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalops.cases.dsl import load_project
from evalops.cli import app
from evalops.config import sqlite_url
from evalops.db.schema import ensure_schema
from evalops.db.session import Database
from evalops.diff import DiffError, diff_runs, diff_steps, load_case_trace
from evalops.runner import RunOutcome, execute_run

runner = CliRunner()


@pytest.fixture()
def diff_db(tmp_path: Path) -> Database:
    url = sqlite_url(tmp_path / "diff.db")
    ensure_schema(url)
    return Database.create(url)


def _step(index: int, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "step": index,
        "type": "tool",
        "name": f"tool_{index}",
        "input": {"index": index},
        "output": {"ok": True},
        "tokens": 0,
        "latency_ms": 10,
    }
    payload.update(overrides)
    return payload


def test_diff_steps_marks_same_changed_and_added() -> None:
    baseline = [_step(1), _step(2)]
    candidate = [_step(1), _step(2, output={"ok": False}), _step(3)]

    diffs = diff_steps(baseline, candidate)

    assert [item["status"] for item in diffs] == ["same", "changed", "added"]
    assert diffs[1]["changes"] == [
        {"field": "output", "baseline": '{"ok": true}', "candidate": '{"ok": false}'}
    ]


def test_diff_steps_marks_removed() -> None:
    diffs = diff_steps([_step(1), _step(2)], [_step(1)])
    assert diffs[-1]["status"] == "removed"


async def _run(db: Database, workdir: Path) -> RunOutcome:
    project = load_project(workdir / "projects" / "fake.yaml")
    return await execute_run(db, project, workdir / "cases")


def _mutate_refund_fixture(workdir: Path) -> None:
    fixture = workdir / "fixtures" / "refund_policy.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["output"] = "退款需要人工审核，请通过客服提交申请。"
    payload["steps"][-1]["output"] = payload["output"]
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


async def test_diff_runs_reports_changed_case(
    workdir: Path, diff_db: Database
) -> None:
    baseline = await _run(diff_db, workdir)
    _mutate_refund_fixture(workdir)
    candidate = await _run(diff_db, workdir)

    async with diff_db.sessionmaker() as session:
        summary = await diff_runs(session, baseline.run_id, candidate.run_id)

    assert summary["detailed"] is False
    entry = next(
        item for item in summary["cases"] if item["case_key"] == "refund_policy"
    )
    assert entry["changed"] is True
    assert entry["baseline_status"] == "passed"
    assert entry["candidate_status"] == "failed"
    assert entry["step_changes"] >= 1
    assert entry["assertion_changes"]
    assert "steps" not in entry
    assert summary["changed_cases"] >= 1

    async with diff_db.sessionmaker() as session:
        detailed = await diff_runs(
            session, baseline.run_id, candidate.run_id, "refund_policy"
        )
    assert detailed["detailed"] is True
    assert len(detailed["cases"]) == 1
    steps = detailed["cases"][0]["steps"]
    assert any(item["status"] == "changed" for item in steps)


async def test_diff_runs_rejects_unknown_case(
    workdir: Path, diff_db: Database
) -> None:
    outcome = await _run(diff_db, workdir)
    async with diff_db.sessionmaker() as session:
        with pytest.raises(DiffError, match="没有用例"):
            await diff_runs(session, outcome.run_id, outcome.run_id, "missing_case")


async def test_load_case_trace(workdir: Path, diff_db: Database) -> None:
    outcome = await _run(diff_db, workdir)
    async with diff_db.sessionmaker() as session:
        trace = await load_case_trace(session, outcome.run_id, "refund_policy")
        assert trace is not None
        assert trace["status"] == "passed"
        assert len(trace["trajectory"]) == 4
        assert trace["assertions"]
        assert await load_case_trace(session, outcome.run_id, "missing") is None
        assert await load_case_trace(session, "nope", "refund_policy") is None


def test_cli_diff_and_trace(workdir: Path) -> None:
    base_args = [
        "run",
        "--json",
        "--project",
        str(workdir / "projects" / "fake.yaml"),
        "--cases",
        str(workdir / "cases"),
    ]
    baseline = runner.invoke(app, [*base_args, "--baseline"])
    assert baseline.exit_code == 0, baseline.output
    _mutate_refund_fixture(workdir)
    candidate = runner.invoke(app, base_args)
    assert candidate.exit_code == 0, candidate.output
    candidate_payload = json.loads(candidate.output)

    diff = runner.invoke(app, ["diff", "--case", "refund_policy", "--json"])
    assert diff.exit_code == 0, diff.output
    payload = json.loads(diff.output)
    assert payload["changed_cases"] == 1
    assert payload["cases"][0]["case_key"] == "refund_policy"

    trace = runner.invoke(
        app,
        ["trace", "--run", candidate_payload["run_id"], "--case", "refund_policy", "--json"],
    )
    assert trace.exit_code == 0, trace.output
    assert json.loads(trace.output)["case_key"] == "refund_policy"
