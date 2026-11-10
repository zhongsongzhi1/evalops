"""run 状态机回归：任何退出路径都必须落在终态，不能停在 running。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

import evalops.runner as runner_module
from evalops.cases.dsl import load_project
from evalops.config import sqlite_url
from evalops.db.models import Baseline, Run
from evalops.db.schema import ensure_schema
from evalops.db.session import Database
from evalops.runner import execute_run


@pytest.fixture()
def status_db(tmp_path: Path) -> Database:
    url = sqlite_url(tmp_path / "status.db")
    ensure_schema(url)
    return Database.create(url)


def _project(workdir: Path):
    return load_project(workdir / "projects" / "fake.yaml")


async def test_crash_still_ends_in_failed_status(
    workdir: Path, status_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = status_db

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(runner_module, "_execute_case", boom)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            await execute_run(db, _project(workdir), workdir / "cases")

        async with db.sessionmaker() as session:
            run = (await session.scalars(select(Run))).one()
            assert run.status == "failed"
            assert run.finished_at is not None
    finally:
        await db.dispose()


async def test_errored_run_is_failed_and_not_baseline(
    workdir: Path, status_db: Database
) -> None:
    for fixture in (workdir / "fixtures").glob("*.json"):
        fixture.unlink()
    db = status_db
    try:
        outcome = await execute_run(
            db, _project(workdir), workdir / "cases", mark_baseline=True
        )
        assert outcome.status == "failed"
        assert outcome.errored == outcome.total

        async with db.sessionmaker() as session:
            run = (await session.scalars(select(Run))).one()
            assert run.status == "failed"
            assert run.finished_at is not None
            baselines = await session.scalar(select(func.count()).select_from(Baseline))
            assert baselines == 0
    finally:
        await db.dispose()


async def test_succeeded_run_can_be_baseline(
    workdir: Path, status_db: Database
) -> None:
    db = status_db
    try:
        outcome = await execute_run(
            db, _project(workdir), workdir / "cases", mark_baseline=True
        )
        assert outcome.status == "succeeded"

        async with db.sessionmaker() as session:
            run = (await session.scalars(select(Run))).one()
            assert run.status == "succeeded"
            assert run.finished_at is not None
            baselines = await session.scalar(select(func.count()).select_from(Baseline))
            assert baselines == 1
    finally:
        await db.dispose()
