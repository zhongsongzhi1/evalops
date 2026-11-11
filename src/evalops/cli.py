"""EvalOps 命令行入口：run / compare / gate / flaky / metrics / diff。"""

from __future__ import annotations

import asyncio
import json
import uuid
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
from evalops.diff import DiffError, diff_runs, load_case_trace
from evalops.flaky import FlakyError, compute_flaky, resolve_run_refs, runs_for_group
from evalops.metrics import project_trend
from evalops.regression import (
    RegressionError,
    RegressionReport,
    compare_runs,
    load_gate,
    resolve_runs,
)
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
    repeat: int = typer.Option(
        1,
        "--repeat",
        min=1,
        max=10,
        help="同一用例集重复执行次数；≥3 时给出 Flaky Rate",
    ),
    db: str | None = typer.Option(None, "--db", help="SQLite 路径或数据库 URL"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """执行一次评测；``--repeat`` 会生成同组多次运行并给出 Flaky Rate。"""
    url = _prepare_db(db)
    tags = [item.strip() for item in tag.split(",") if item.strip()] if tag else None
    try:
        loaded_project = load_project(project)
    except DslError as exc:
        _fail(str(exc))

    async def _run() -> tuple[list[RunOutcome], str | None, dict[str, Any] | None]:
        database = Database.create(url)
        try:
            group = uuid.uuid4().hex if repeat > 1 else None
            outcomes: list[RunOutcome] = []
            for index in range(repeat):
                outcomes.append(
                    await execute_run(
                        database,
                        loaded_project,
                        cases,
                        tags=tags,
                        trigger=trigger,
                        git_sha=git_sha,
                        prompt_version=prompt_version,
                        mark_baseline=baseline and index == 0,
                        baseline_label=baseline_label,
                        repeat_group=group,
                        repeat_index=index,
                    )
                )
            flaky_payload = None
            if group is not None and repeat >= 3:
                async with database.sessionmaker() as session:
                    report = await compute_flaky(
                        session, [item.run_id for item in outcomes]
                    )
                    flaky_payload = report.to_dict()
            return outcomes, group, flaky_payload
        finally:
            await database.dispose()

    try:
        outcomes, group, flaky_payload = asyncio.run(_run())
    except DslError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - 命令行统一报错
        _fail(f"评测失败: {exc}")

    if as_json:
        if repeat == 1:
            payload: dict[str, Any] = outcomes[0].to_dict()
        else:
            payload = {
                "group": group,
                "repeat": repeat,
                "runs": [item.to_dict() for item in outcomes],
                "flaky": flaky_payload,
            }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for outcome in outcomes:
            _echo_run(outcome)
        if flaky_payload is not None:
            typer.echo(
                f"重复运行 {repeat} 次 | Flaky Rate {flaky_payload['flaky_rate']:.1%}"
                f"（{len(flaky_payload['flaky_cases'])}/{flaky_payload['total_cases']}）"
            )
            for item in flaky_payload["flaky_cases"]:
                typer.echo(f"  - {item['case_key']}: {' -> '.join(item['statuses'])}")
        elif repeat > 1:
            typer.echo(f"重复 {repeat} 次不足以判定 Flaky（需要 ≥3 次）")
        if baseline:
            typer.echo("已记录为基线。下一步：evalops gate --candidate latest")
    if any(item.errored for item in outcomes):
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


@app.command("flaky")
def flaky_command(
    runs: str | None = typer.Option(None, "--runs", help="逗号分隔的运行 ID/前缀"),
    group: str | None = typer.Option(None, "--group", help="重复运行组 ID（或组内任一运行）"),
    db: str | None = typer.Option(None, "--db", help="SQLite 路径或数据库 URL"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """统计参与重跑用例的 Flaky Rate 与不稳定断言。"""
    if (runs is None) == (group is None):
        _fail("--runs 与 --group 必须二选一")
    url = _prepare_db(db)

    async def _flaky() -> dict[str, Any]:
        database = Database.create(url)
        try:
            async with database.sessionmaker() as session:
                if runs is not None:
                    selected = await resolve_run_refs(session, runs)
                elif group is not None:
                    selected = await runs_for_group(session, group)
                else:  # pragma: no cover - 已在上面校验
                    raise FlakyError("--runs 与 --group 必须二选一")
                report = await compute_flaky(session, [item.id for item in selected])
                return report.to_dict()
        finally:
            await database.dispose()

    try:
        payload = asyncio.run(_flaky())
    except FlakyError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(f"Flaky 计算失败: {exc}")

    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    typer.echo(
        f"运行 {len(payload['run_ids'])} 次 | 参与用例 {payload['total_cases']} | "
        f"Flaky {len(payload['flaky_cases'])} | Flaky Rate {payload['flaky_rate']:.1%}"
    )
    for item in payload["flaky_cases"]:
        unstable = ", ".join(
            f"#{assertion['seq']} {assertion['type']}"
            for assertion in item["unstable_assertions"]
        ) or "状态摆动"
        typer.echo(f"  - {item['case_key']}: {' -> '.join(item['statuses'])}（{unstable}）")
    if payload["skipped_cases"]:
        typer.echo(f"  跳过 {len(payload['skipped_cases'])} 条未参与全部重跑的用例")


@app.command("metrics")
def metrics_command(
    project: str = typer.Option(..., "--project", "-p", help="项目名或 project.yaml 路径"),
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="最近 N 次运行"),
    db: str | None = typer.Option(None, "--db", help="SQLite 路径或数据库 URL"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """按时间输出 Pass Rate、断言通过率、p95 与单用例成本趋势。"""
    url = _prepare_db(db)

    async def _metrics() -> list[dict[str, Any]]:
        database = Database.create(url)
        try:
            async with database.sessionmaker() as session:
                project_id = await _resolve_project_id(session, project)
                if project_id is None:  # pragma: no cover - project 必填
                    raise CalibrationError("--project 不能为空")
                trend = await project_trend(session, project_id, limit)
                return [item.to_dict() for item in trend]
        finally:
            await database.dispose()

    try:
        payload = asyncio.run(_metrics())
    except CalibrationError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(f"指标计算失败: {exc}")

    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    typer.echo(
        f"{'run':10} {'时间':16} {'Pass':>7} {'断言':>7} {'p95':>9} {'单例成本':>11}"
    )
    for item in payload:
        stamp = (item["started_at"] or "")[:19].replace("T", " ") or "-"
        typer.echo(
            f"{item['run_id'][:8]:10} {stamp:16} "
            f"{item['pass_rate']:>7.1%} {item['assertion_pass_rate']:>7.1%} "
            f"{item['p95_latency_ms']:>7}ms ${item['cost_per_case']:>9.6f}"
        )


@app.command("diff")
def diff_command(
    baseline: str = typer.Option("baseline", "--baseline", help="基线运行 ID，或 baseline"),
    candidate: str = typer.Option("latest", "--candidate", help="候选运行 ID，或 latest"),
    case: str | None = typer.Option(
        None, "--case", help="只对比指定 case_key，并输出逐步轨迹差异"
    ),
    db: str | None = typer.Option(None, "--db", help="SQLite 路径或数据库 URL"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """对比两次运行的轨迹与断言差异。"""
    url = _prepare_db(db)

    async def _diff() -> dict[str, Any]:
        database = Database.create(url)
        try:
            async with database.sessionmaker() as session:
                base_run, cand_run = await resolve_runs(session, baseline, candidate)
                return await diff_runs(session, base_run.id, cand_run.id, case)
        finally:
            await database.dispose()

    try:
        payload = asyncio.run(_diff())
    except (DiffError, RegressionError) as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(f"轨迹对比失败: {exc}")

    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    typer.echo(
        f"对比 {payload['baseline_run_id'][:8]} -> {payload['candidate_run_id'][:8]} | "
        f"变更 {payload['changed_cases']}/{len(payload['cases'])} 条"
    )
    for item in payload["cases"]:
        if not item["changed"]:
            continue
        typer.echo(
            f"- {item['case_key']}: {item['baseline_status']} -> {item['candidate_status']} "
            f"| 步骤变化 {item['step_changes']} | 断言变化 {len(item['assertion_changes'])}"
        )
        for step in item.get("steps", []):
            if step["status"] == "same":
                continue
            typer.echo(f"    #{step['step']} {step['status']}")
            for change in step["changes"]:
                typer.echo(
                    f"      {change['field']}: {change['baseline']} -> {change['candidate']}"
                )


@app.command("trace")
def trace_command(
    run: str = typer.Option(..., "--run", help="运行 ID、唯一前缀或 latest"),
    case: str = typer.Option(..., "--case", help="用例 case_key"),
    db: str | None = typer.Option(None, "--db", help="SQLite 路径或数据库 URL"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """回放单用例的完整轨迹与断言结果。"""
    url = _prepare_db(db)

    async def _trace() -> dict[str, Any] | None:
        database = Database.create(url)
        try:
            async with database.sessionmaker() as session:
                resolved = await resolve_run_ref(session, run)
                return await load_case_trace(session, resolved.id, case)
        finally:
            await database.dispose()

    try:
        payload = asyncio.run(_trace())
    except CalibrationError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(f"轨迹回放失败: {exc}")

    if payload is None:
        _fail(f"运行中没有用例: {case}")

    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    typer.echo(
        f"{payload['case_key']} | {payload['status']} | "
        f"{payload['latency_ms']}ms | ${payload['cost_usd']:.6f}"
    )
    for step in payload["trajectory"]:
        typer.echo(
            f"  #{step.get('step')} {step.get('type')} {step.get('name', '')} "
            f"{step.get('latency_ms', 0)}ms"
        )
        for field in ("input", "output"):
            value = json.dumps(step.get(field), ensure_ascii=False, default=str)
            typer.echo(f"    {field}: {value[:200]}")
    typer.echo("断言:")
    for assertion in payload["assertions"]:
        typer.echo(
            f"  #{assertion['seq']} {assertion['type']} "
            f"{'passed' if assertion['passed'] else 'failed'}"
        )


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
