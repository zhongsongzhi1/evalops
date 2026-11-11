"""阶段 3 稳定性：重复运行分组、Flaky 判定与工具录制落库。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from evalops.cases.dsl import load_project
from evalops.cli import app
from evalops.config import sqlite_url
from evalops.db.models import (
    AssertionResult,
    Case,
    CaseResult,
    CaseSet,
    Project,
    Run,
    ToolRecording,
)
from evalops.db.schema import ensure_schema
from evalops.db.session import Database
from evalops.flaky import FlakyError, compute_flaky, runs_for_group
from evalops.runner import execute_run

runner = CliRunner()


@pytest.fixture()
def stability_db(tmp_path: Path) -> Database:
    url = sqlite_url(tmp_path / "stability.db")
    ensure_schema(url)
    return Database.create(url)


async def _seed(
    session: AsyncSession, snapshots: list[tuple[str, bool | None]]
) -> list[str]:
    """写入同一 case_key 的多次运行；``passed=None`` 表示该次没有断言结果。"""
    project = Project(name="flaky-demo", adapter="fake", config={})
    session.add(project)
    await session.flush()
    case_set = CaseSet(project_id=project.id, version=1, content_hash="hash-1")
    session.add(case_set)
    await session.flush()
    case = Case(
        case_set_id=case_set.id,
        case_key="case_a",
        name="抖动用例",
        input={},
        expected={},
        assertions=[],
        tags=[],
        enabled=True,
    )
    session.add(case)
    await session.flush()

    run_ids: list[str] = []
    for status, passed in snapshots:
        run = Run(
            project_id=project.id,
            case_set_id=case_set.id,
            trigger="manual",
            config_snapshot={},
            status="succeeded",
        )
        session.add(run)
        await session.flush()
        result = CaseResult(
            run_id=run.id, case_id=case.id, status=status, latency_ms=100
        )
        session.add(result)
        await session.flush()
        if passed is not None:
            session.add(
                AssertionResult(
                    case_result_id=result.id, seq=1, type="contains", passed=passed
                )
            )
        run_ids.append(run.id)
    await session.commit()
    return run_ids


async def test_compute_flaky_detects_unstable_assertion(stability_db: Database) -> None:
    async with stability_db.sessionmaker() as session:
        run_ids = await _seed(
            session, [("passed", True), ("passed", False), ("passed", True)]
        )
        report = await compute_flaky(session, run_ids)

    assert report.run_ids == run_ids
    assert report.total_cases == 1
    assert report.stable_cases == 0
    assert report.flaky_rate == 1.0
    flake = report.flaky_cases[0]
    assert flake.case_key == "case_a"
    assert flake.statuses == ["passed", "passed", "passed"]
    assert flake.unstable_assertions == [
        {"seq": 1, "type": "contains", "values": [True, False, True]}
    ]


async def test_compute_flaky_marks_stable_case(stability_db: Database) -> None:
    async with stability_db.sessionmaker() as session:
        run_ids = await _seed(
            session, [("passed", True), ("passed", True), ("passed", True)]
        )
        report = await compute_flaky(session, run_ids)

    assert report.total_cases == 1
    assert report.stable_cases == 1
    assert report.flaky_cases == []
    assert report.flaky_rate == 0.0


async def test_compute_flaky_detects_status_swing(stability_db: Database) -> None:
    async with stability_db.sessionmaker() as session:
        run_ids = await _seed(
            session, [("passed", True), ("error", None), ("passed", True)]
        )
        report = await compute_flaky(session, run_ids)

    assert report.flaky_rate == 1.0
    flake = report.flaky_cases[0]
    assert flake.statuses == ["passed", "error", "passed"]
    assert flake.unstable_assertions == []


async def test_compute_flaky_requires_three_runs(stability_db: Database) -> None:
    async with stability_db.sessionmaker() as session:
        run_ids = await _seed(session, [("passed", True), ("passed", True)])
        with pytest.raises(FlakyError, match="至少需要三次运行"):
            await compute_flaky(session, run_ids)


def test_repeat_run_groups_and_flaky_cli(workdir: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--repeat",
            "3",
            "--json",
            "--project",
            str(workdir / "projects" / "fake.yaml"),
            "--cases",
            str(workdir / "cases"),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["repeat"] == 3
    assert len(payload["runs"]) == 3
    assert payload["flaky"]["total_cases"] == 4
    assert payload["flaky"]["flaky_rate"] == 0.0

    group = payload["group"]
    assert group
    report = runner.invoke(app, ["flaky", "--group", group, "--json"])
    assert report.exit_code == 0, report.output
    parsed = json.loads(report.output)
    assert parsed["run_ids"] == [item["run_id"] for item in payload["runs"]]
    assert parsed["flaky_cases"] == []


async def test_runs_for_group_orders_by_repeat_index(
    workdir: Path, stability_db: Database
) -> None:
    project = load_project(workdir / "projects" / "fake.yaml")
    first = await execute_run(
        stability_db, project, workdir / "cases", repeat_group="group-x", repeat_index=0
    )
    second = await execute_run(
        stability_db, project, workdir / "cases", repeat_group="group-x", repeat_index=1
    )

    async with stability_db.sessionmaker() as session:
        runs = await runs_for_group(session, "group-x")
        assert [run.id for run in runs] == [first.run_id, second.run_id]
        assert [run.config_snapshot["repeat_index"] for run in runs] == [0, 1]
        by_run = await runs_for_group(session, second.run_id)
        assert [run.id for run in by_run] == [first.run_id, second.run_id]


async def test_tool_recordings_are_persisted(
    workdir: Path, stability_db: Database
) -> None:
    project = load_project(workdir / "projects" / "fake.yaml")
    await execute_run(stability_db, project, workdir / "cases")

    async with stability_db.sessionmaker() as session:
        recordings = (await session.scalars(select(ToolRecording))).all()

    assert len(recordings) == 4
    assert {item.source for item in recordings} == {"fixture"}
    assert all(len(item.request_digest) == 64 for item in recordings)
    assert all(item.truncated is False for item in recordings)
    lookup = [item for item in recordings if item.tool == "query_order"]
    assert lookup[0].response["order_id"] == "A1B2C3"
