"""EvalOps 命令行入口：run / compare / gate。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, NoReturn

import typer
from sqlalchemy import select

from evalops.calibration import (
    MIN_CALIBRATION_SAMPLES,
    CalibrationError,
    KappaResult,
    compute_project_kappa,
    import_labels,
    resolve_run_ref,
)
from evalops.cases.dsl import DslError, load_project
from evalops.config import resolve_db_url
from evalops.db.models import Project
from evalops.db.schema import ensure_schema
from evalops.db.session import Database
from evalops.regression import RegressionError, RegressionReport, compare_runs, load_gate
from evalops.runner import RunOutcome, execute_run

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="EvalOps：Agent 评测、回归对比与 CI 门禁",
)


def main() -> None:
    app()


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _prepare_db(db: str | None) -> str:
    url = resolve_db_url(db)
    try:
        ensure_schema(url)
    except Exception as exc:  # noqa: BLE001 - 数据库初始化失败直接退出
        _fail(f"数据库初始化失败: {exc}")
    return url


def _echo_run(outcome: RunOutcome) -> None:
    typer.echo(f"运行完成 run_id={outcome.run_id}")
    typer.echo(
        f"用例 {outcome.total} | 通过 {outcome.passed} | 失败 {outcome.failed} | "
        f"错误 {outcome.errored} | Pass Rate {outcome.pass_rate:.1%} | "
        f"断言通过率 {outcome.assertion_pass_rate:.1%}"
    )
    typer.echo(
        f"成本 ${outcome.cost_usd:.6f} | 耗时 {outcome.duration_ms}ms | 状态 {outcome.status}"
    )
    for case in outcome.cases:
        typer.echo(
            f"  {case.status.upper():<6} {case.case_key:<24} {case.latency_ms:>6}ms  {case.name}"
        )
        for failed in case.failed_assertions:
            detail = json.dumps(failed["detail"], ensure_ascii=False)
            typer.echo(f"         - 断言 #{failed['seq']} {failed['type']}: {detail}")
        if case.error:
            typer.echo(f"         - 错误: {case.error}")


def _echo_report(report: RegressionReport, gate_name: str) -> None:
    typer.echo(
        f"基线 {report.baseline_run_id}  Pass Rate {report.baseline_pass_rate:.1%}  "
        f"断言通过率 {report.baseline_assertion_pass_rate:.1%}"
    )
    typer.echo(
        f"候选 {report.candidate_run_id}  Pass Rate {report.candidate_pass_rate:.1%}  "
        f"断言通过率 {report.candidate_assertion_pass_rate:.1%}"
    )
    typer.echo(f"变化 {report.delta:+.1%} | 成本变化 {report.cost_delta_ratio:+.1%}")
    if report.judge_kappa is not None:
        typer.echo(
            f"Judge κ {report.judge_kappa:.2f}"
            f"（加权 {report.judge_kappa_weighted or 0:.2f}，n={report.judge_kappa_n}）"
        )
    if report.new_failures:
        typer.echo(
            f"New Failures ({len(report.new_failures)}): {', '.join(report.new_failures)}"
        )
    if report.fixed:
        typer.echo(f"已修复: {', '.join(report.fixed)}")
    if report.added:
        typer.echo(f"新增用例: {', '.join(report.added)}")
    if report.removed:
        typer.echo(f"移除用例: {', '.join(report.removed)}")
    color = {"pass": typer.colors.GREEN, "warn": typer.colors.YELLOW}.get(
        report.verdict, typer.colors.RED
    )
    typer.secho(f"门禁 {gate_name}: {report.verdict.upper()}", fg=color, bold=True)
    for reason in report.reasons:
        typer.echo(f"  - {reason}")
    for note in report.notes:
        typer.echo(f"  注: {note}")


@app.command("run")
def run_command(
    project: Path = typer.Option(
        ..., "--project", "-p", exists=True, dir_okay=False, help="被测项目配置 YAML"
    ),
    cases: Path = typer.Option(
        Path("cases"), "--cases", "-c", exists=True, help="用例目录或单个 YAML"
    ),
    tag: str | None = typer.Option(None, "--tag", "--tags", help="逗号分隔，命中任一标签即执行"),
    baseline: bool = typer.Option(False, "--baseline", help="把本次运行标记为项目基线"),
    baseline_label: str = typer.Option("latest", "--baseline-label", help="基线标签"),
    trigger: str = typer.Option("manual", "--trigger", help="触发来源：manual / ci"),
    git_sha: str | None = typer.Option(None, "--git-sha", help="被测代码提交号"),
    prompt_version: str | None = typer.Option(None, "--prompt-version", help="Prompt 版本"),
    db: str | None = typer.Option(None, "--db", help="SQLite 路径或数据库 URL"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """执行一次评测。"""
    url = _prepare_db(db)
    tags = [item.strip() for item in tag.split(",") if item.strip()] if tag else None
    try:
        loaded_project = load_project(project)
    except DslError as exc:
        _fail(str(exc))

    async def _run() -> RunOutcome:
        database = Database.create(url)
        try:
            return await execute_run(
                database,
                loaded_project,
                cases,
                tags=tags,
                trigger=trigger,
                git_sha=git_sha,
                prompt_version=prompt_version,
                mark_baseline=baseline,
                baseline_label=baseline_label,
            )
        finally:
            await database.dispose()

    try:
        outcome = asyncio.run(_run())
    except DslError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - 命令行统一报错
        _fail(f"评测失败: {exc}")

    if as_json:
        typer.echo(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2))
    else:
        _echo_run(outcome)
        if baseline:
            typer.echo("已记录为基线。下一步：evalops gate --candidate latest")
    if outcome.errored:
        raise typer.Exit(1)


@app.command("compare")
def compare_command(
    baseline: str = typer.Option("baseline", "--baseline", help="基线运行 ID，或 baseline"),
    candidate: str = typer.Option("latest", "--candidate", help="候选运行 ID，或 latest"),
    gate_file: Path | None = typer.Option(
        None, "--gate", exists=True, dir_okay=False, help="门禁规则 YAML"
    ),
    db: str | None = typer.Option(None, "--db", help="SQLite 路径或数据库 URL"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """对比基线与候选，列出 New Failures。"""
    url = _prepare_db(db)
    try:
        gate = load_gate(gate_file)
    except RegressionError as exc:
        _fail(str(exc))

    async def _compare() -> RegressionReport:
        database = Database.create(url)
        try:
            return await compare_runs(database, baseline, candidate, gate)
        finally:
            await database.dispose()

    try:
        report = asyncio.run(_compare())
    except RegressionError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(f"对比失败: {exc}")

    if as_json:
        typer.echo(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        _echo_report(report, gate.name)


@app.command("gate")
def gate_command(
    baseline: str = typer.Option("baseline", "--baseline", help="基线运行 ID，或 baseline"),
    candidate: str = typer.Option("latest", "--candidate", help="候选运行 ID，或 latest"),
    gate_file: Path | None = typer.Option(
        None, "--gate", exists=True, dir_okay=False, help="门禁规则 YAML"
    ),
    db: str | None = typer.Option(None, "--db", help="SQLite 路径或数据库 URL"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """执行门禁判定：pass / warn 退出码 0，block 退出码 2。"""
    url = _prepare_db(db)
    try:
        gate = load_gate(gate_file)
    except RegressionError as exc:
        _fail(str(exc))

    async def _gate() -> RegressionReport:
        database = Database.create(url)
        try:
            return await compare_runs(database, baseline, candidate, gate)
        finally:
            await database.dispose()

    try:
        report = asyncio.run(_gate())
    except RegressionError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(f"门禁执行失败: {exc}")

    if as_json:
        typer.echo(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        _echo_report(report, gate.name)
    if report.verdict == "block":
        raise typer.Exit(2)


def _echo_kappa(result: KappaResult, threshold: float) -> bool:
    typer.echo(
        f"Judge κ {result.kappa:.4f}（二次加权 {result.kappa_weighted:.4f}）| "
        f"样本 {result.n} | 一致率 {result.observed_agreement:.1%}"
    )
    ok = result.kappa >= threshold
    color = typer.colors.GREEN if ok else typer.colors.RED
    typer.secho(f"阈值 {threshold:.2f}: {'达标' if ok else '未达标'}", fg=color, bold=True)
    if not result.sufficient:
        typer.echo(f"  注: 样本少于 {MIN_CALIBRATION_SAMPLES} 条，结论仅供参考")
    return ok


async def _resolve_project_id(session: Any, project: str | None) -> str | None:
    if project is None:
        return None
    path = Path(project)
    if path.suffix in (".yaml", ".yml") or path.is_file():
        try:
            name = load_project(path).spec.name
        except DslError as exc:
            raise CalibrationError(str(exc)) from exc
    else:
        name = project
    row = await session.scalar(select(Project).where(Project.name == name))
    if row is None:
        raise CalibrationError(f"项目不存在: {name}")
    return row.id


@app.command("label")
def label_command(
    run: str = typer.Option(..., "--run", help="运行 ID、唯一前缀或 latest"),
    csv_file: Path = typer.Option(
        ..., "--csv", exists=True, dir_okay=False, help="标注 CSV：case_key,human_score[,notes]"
    ),
    labeled_by: str = typer.Option("human", "--labeled-by", help="标注人标识"),
    db: str | None = typer.Option(None, "--db", help="SQLite 路径或数据库 URL"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """导入人工标注，绑定到本次运行的 Judge 输出。"""
    url = _prepare_db(db)

    async def _import() -> Any:
        database = Database.create(url)
        try:
            async with database.sessionmaker() as session:
                resolved = await resolve_run_ref(session, run)
                return await import_labels(session, resolved.id, csv_file, labeled_by)
        finally:
            await database.dispose()

    try:
        result = asyncio.run(_import())
    except CalibrationError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(f"导入标注失败: {exc}")

    if as_json:
        typer.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(
            f"导入 {result.imported} 条 | 更新 {result.updated} 条 | 跳过 {result.skipped} 条"
        )
        for error in result.errors:
            typer.echo(f"  - {error}")
    if result.skipped and not result.imported and not result.updated:
        raise typer.Exit(1)


@app.command("kappa")
def kappa_command(
    project: str | None = typer.Option(
        None, "--project", "-p", help="项目名或 project.yaml 路径；缺省统计全部标注"
    ),
    rubric_version: str | None = typer.Option(
        None, "--rubric-version", help="只统计指定 rubric 版本"
    ),
    threshold: float = typer.Option(0.6, "--threshold", min=0.0, max=1.0, help="κ 达标阈值"),
    db: str | None = typer.Option(None, "--db", help="SQLite 路径或数据库 URL"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """计算 Judge 与人工评分的一致性（Cohen's κ）。"""
    url = _prepare_db(db)

    async def _kappa() -> KappaResult | None:
        database = Database.create(url)
        try:
            async with database.sessionmaker() as session:
                project_id = await _resolve_project_id(session, project)
                return await compute_project_kappa(session, project_id, rubric_version)
        finally:
            await database.dispose()

    try:
        result = asyncio.run(_kappa())
    except CalibrationError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(f"计算 κ 失败: {exc}")

    if result is None:
        _fail("还没有人工标注；先执行 evalops label --run <run_id> --csv labels.csv")

    if as_json:
        payload = result.to_dict()
        payload["threshold"] = threshold
        payload["passed_threshold"] = result.kappa >= threshold
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _echo_kappa(result, threshold)
    if result.kappa < threshold:
        raise typer.Exit(1)


@app.command("serve")
def serve_command(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8000, "--port", help="监听端口"),
    db: str | None = typer.Option(None, "--db", help="SQLite 路径或数据库 URL"),
) -> None:
    """启动本地评测控制台（需要安装 web 依赖）。"""
    try:
        import uvicorn
    except ImportError:
        _fail('未安装 Web 依赖，请执行: pip install -e ".[web]"')
    from evalops.api.app import create_app

    url = resolve_db_url(db)
    typer.echo(f"控制台: http://{host}:{port}  数据库: {url}")
    uvicorn.run(create_app(url), host=host, port=port, log_level="info")
