"""EvalOps 命令行入口：run / compare / gate。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import NoReturn

import typer

from evalops.cases.dsl import DslError, load_project
from evalops.config import resolve_db_url
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
