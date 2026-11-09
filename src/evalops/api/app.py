"""最小控制面：给静态前端提供运行查询、对比与触发入口。

刻意保持只读为主：配置仍由 YAML / CLI 管理，这里只暴露页面需要的查询，
外加一个触发评测的接口，方便本地演示。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select

from evalops.cases.dsl import DslError, load_project
from evalops.config import resolve_db_url
from evalops.db.models import AssertionResult, Baseline, Case, CaseResult, Project, Run
from evalops.db.schema import ensure_schema
from evalops.db.session import Database
from evalops.regression import (
    RegressionError,
    apply_gate,
    build_report,
    load_gate,
    resolve_runs,
)
from evalops.runner import execute_run

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


class RunRequest(BaseModel):
    project: str
    cases: str
    tags: list[str] | None = None
    baseline: bool = False
    trigger: str = "manual"


class BaselineRequest(BaseModel):
    label: str = "latest"


def _database(request: Request) -> Database:
    database: Database = request.app.state.db
    return database


def create_app(db: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        url = resolve_db_url(db)
        # Alembic 的异步 env 内部会调用 asyncio.run，必须放到工作线程执行
        await asyncio.to_thread(ensure_schema, url)
        app.state.db = Database.create(url)
        yield
        await app.state.db.dispose()

    app = FastAPI(title="EvalOps API", version="0.1.0", lifespan=lifespan)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/runs")
    async def list_runs(
        request: Request,
        limit: int = 50,
        database: Database = Depends(_database),
    ) -> list[dict[str, Any]]:
        return await _list_runs(database, max(1, min(limit, 200)))

    @app.get("/api/runs/{run_id}")
    async def get_run(
        run_id: str,
        database: Database = Depends(_database),
    ) -> dict[str, Any]:
        return await _run_detail(database, run_id)

    @app.post("/api/runs")
    async def create_run(
        body: RunRequest,
        database: Database = Depends(_database),
    ) -> dict[str, Any]:
        project_path = Path(body.project)
        if not project_path.is_file():
            raise HTTPException(status_code=400, detail=f"项目配置不存在: {project_path}")
        if not Path(body.cases).exists():
            raise HTTPException(status_code=400, detail=f"用例路径不存在: {body.cases}")
        try:
            loaded = load_project(project_path)
            outcome = await execute_run(
                database,
                loaded,
                body.cases,
                tags=body.tags,
                trigger=body.trigger,
                mark_baseline=body.baseline,
            )
        except DslError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return outcome.to_dict()

    @app.post("/api/runs/{run_id}/baseline")
    async def mark_baseline(
        run_id: str,
        body: BaselineRequest,
        database: Database = Depends(_database),
    ) -> dict[str, Any]:
        async with database.sessionmaker() as session:
            run = await session.get(Run, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="运行不存在")
            session.add(Baseline(project_id=run.project_id, run_id=run.id, label=body.label))
            await session.commit()
        return {"ok": True, "run_id": run_id, "label": body.label}

    @app.get("/api/compare")
    async def compare(
        baseline: str = "baseline",
        candidate: str = "latest",
        database: Database = Depends(_database),
    ) -> dict[str, Any]:
        async with database.sessionmaker() as session:
            try:
                base, cand = await resolve_runs(session, baseline, candidate)
                report = await build_report(session, base.id, cand.id)
            except RegressionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        report = apply_gate(report, load_gate(None))
        return report.to_dict()

    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


async def _list_runs(database: Database, limit: int) -> list[dict[str, Any]]:
    async with database.sessionmaker() as session:
        rows = (
            await session.execute(
                select(Run, Project.name)
                .join(Project, Run.project_id == Project.id)
                .order_by(Run.started_at.desc())
                .limit(limit)
            )
        ).all()
        run_ids = [run.id for run, _ in rows]
        stats: dict[str, dict[str, int]] = {}
        if run_ids:
            stat_rows = (
                await session.execute(
                    select(CaseResult.run_id, CaseResult.status, func.count())
                    .where(CaseResult.run_id.in_(run_ids))
                    .group_by(CaseResult.run_id, CaseResult.status)
                )
            ).all()
            for run_id, status, count in stat_rows:
                stats.setdefault(run_id, {})[status] = int(count)

    items: list[dict[str, Any]] = []
    for run, project_name in rows:
        counts = stats.get(run.id, {})
        total = sum(counts.values())
        passed = counts.get("passed", 0)
        items.append(
            {
                "id": run.id,
                "project": project_name,
                "status": run.status,
                "trigger": run.trigger,
                "model": run.model,
                "git_sha": run.git_sha,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "cost_usd": run.cost_usd,
                "total": total,
                "passed": passed,
                "failed": counts.get("failed", 0),
                "errored": counts.get("error", 0),
                "pass_rate": passed / total if total else 0.0,
            }
        )
    return items


async def _run_detail(database: Database, run_id: str) -> dict[str, Any]:
    async with database.sessionmaker() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="运行不存在")
        project = await session.get(Project, run.project_id)
        rows = (
            await session.execute(
                select(CaseResult, Case.case_key, Case.name)
                .join(Case, CaseResult.case_id == Case.id)
                .where(CaseResult.run_id == run.id)
            )
        ).all()
        result_ids = [result.id for result, _, _ in rows]
        assertions: list[AssertionResult] = []
        if result_ids:
            assertions = list(
                (
                    await session.execute(
                        select(AssertionResult).where(
                            AssertionResult.case_result_id.in_(result_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )

        failures: dict[str, list[dict[str, Any]]] = {}
        for item in assertions:
            if not item.passed:
                failures.setdefault(item.case_result_id, []).append(
                    {"seq": item.seq, "type": item.type, "detail": item.detail}
                )

        cases: list[dict[str, Any]] = []
        for result, case_key, case_name in rows:
            cases.append(
                {
                    "case_key": case_key,
                    "name": case_name,
                    "status": result.status,
                    "latency_ms": result.latency_ms,
                    "cost_usd": result.cost_usd,
                    "tokens_in": result.tokens_in,
                    "tokens_out": result.tokens_out,
                    "error": result.error,
                    "failed_assertions": failures.get(result.id, []),
                }
            )

    cases.sort(key=lambda item: (item["status"] == "passed", item["case_key"]))
    total = len(cases)
    passed = sum(1 for item in cases if item["status"] == "passed")
    return {
        "id": run.id,
        "project": project.name if project is not None else "",
        "status": run.status,
        "trigger": run.trigger,
        "model": run.model,
        "git_sha": run.git_sha,
        "prompt_version": run.prompt_version,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "cost_usd": run.cost_usd,
        "total": total,
        "passed": passed,
        "failed": sum(1 for item in cases if item["status"] == "failed"),
        "errored": sum(1 for item in cases if item["status"] == "error"),
        "pass_rate": passed / total if total else 0.0,
        "cases": cases,
    }


app = create_app()
