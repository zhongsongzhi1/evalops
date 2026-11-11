"""阶段 3 指标：p95、断言通过率、单用例成本与趋势。"""

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
from evalops.metrics import compute_run_metrics, percentile, project_trend
from evalops.runner import execute_run

runner = CliRunner()


@pytest.fixture()
def metrics_db(tmp_path: Path) -> Database:
    url = sqlite_url(tmp_path / "metrics.db")
    ensure_schema(url)
    return Database.create(url)


def test_percentile_uses_nearest_rank() -> None:
    assert percentile([], 0.95) == 0.0
    assert percentile([7], 0.95) == 7
    assert percentile(list(range(1, 101)), 0.95) == 95
    assert percentile([10, 20, 30], 0.5) == 20


async def test_compute_run_metrics_after_run(
    workdir: Path, metrics_db: Database
) -> None:
    project = load_project(workdir / "projects" / "fake.yaml")
    outcome = await execute_run(metrics_db, project, workdir / "cases")

    async with metrics_db.sessionmaker() as session:
        metrics = await compute_run_metrics(session, outcome.run_id)

    assert metrics is not None
    assert metrics.status == "succeeded"
    assert (metrics.total, metrics.passed, metrics.failed, metrics.errored) == (4, 4, 0, 0)
    assert metrics.pass_rate == 1.0
    assert metrics.assertion_total == outcome.assertion_total
    assert metrics.assertion_pass_rate == 1.0
    assert metrics.cost_usd == pytest.approx(outcome.cost_usd)
    assert metrics.cost_per_case == pytest.approx(outcome.cost_usd / 4)
    assert metrics.p95_latency_ms > 0
    assert metrics.tokens_in == sum(case.tokens_in for case in outcome.cases)


async def test_compute_run_metrics_missing_run(metrics_db: Database) -> None:
    async with metrics_db.sessionmaker() as session:
        assert await compute_run_metrics(session, "nope") is None


async def test_project_trend_is_chronological(
    workdir: Path, metrics_db: Database
) -> None:
    project = load_project(workdir / "projects" / "fake.yaml")
    first = await execute_run(metrics_db, project, workdir / "cases")
    second = await execute_run(metrics_db, project, workdir / "cases")

    async with metrics_db.sessionmaker() as session:
        latest = await project_trend(session, first.project_id, limit=1)
        full = await project_trend(session, first.project_id)

    assert [item.run_id for item in latest] == [second.run_id]
    assert [item.run_id for item in full] == [first.run_id, second.run_id]


def test_metrics_cli_outputs_trend(workdir: Path) -> None:
    base_args = [
        "run",
        "--json",
        "--project",
        str(workdir / "projects" / "fake.yaml"),
        "--cases",
        str(workdir / "cases"),
    ]
    for _ in range(2):
        result = runner.invoke(app, base_args)
        assert result.exit_code == 0, result.output

    trend = runner.invoke(app, ["metrics", "--project", "demo-agent", "--json"])
    assert trend.exit_code == 0, trend.output
    payload = json.loads(trend.output)
    assert len(payload) == 2
    assert payload[-1]["pass_rate"] == 1.0
    assert payload[-1]["assertion_pass_rate"] == 1.0
