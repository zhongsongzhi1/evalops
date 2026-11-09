"""数据模型：设计文档 §3 的 12 张表。

JSON 字段统一使用 ``JSON`` 类型，在 PostgreSQL 上自动落为 ``JSONB``。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONType = JSON().with_variant(JSONB(), "postgresql")


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Project(Base):
    """被测 Agent 的接入配置。"""

    __tablename__ = "project"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    adapter: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CaseSet(Base):
    """用例集快照：导入即不可变，同一项目下按 content_hash 去重。"""

    __tablename__ = "case_set"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_case_set_version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Case(Base):
    """单条用例；``case_key`` 跨版本稳定，回归按它对齐。"""

    __tablename__ = "case"
    __table_args__ = (UniqueConstraint("case_set_id", "case_key", name="uq_case_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    case_set_id: Mapped[str] = mapped_column(ForeignKey("case_set.id"), index=True)
    case_key: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256))
    input: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    expected: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    assertions: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    tags: Mapped[list[str]] = mapped_column(JSONType, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Run(Base):
    """一次完整评测。状态机：queued → running → succeeded / failed / cancelled。"""

    __tablename__ = "run"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    case_set_id: Mapped[str] = mapped_column(ForeignKey("case_set.id"))
    git_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class CaseResult(Base):
    """单用例结果，含完整轨迹。"""

    __tablename__ = "case_result"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("case.id"), index=True)
    status: Mapped[str] = mapped_column(String(16))
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    trajectory: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    error: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class AssertionResult(Base):
    """每条断言的结果，``seq`` 对应 DSL 中编译后的断言序号。"""

    __tablename__ = "assertion_result"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    case_result_id: Mapped[str] = mapped_column(ForeignKey("case_result.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(32))
    passed: Mapped[bool] = mapped_column(Boolean)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class Baseline(Base):
    """被认可的基线运行。"""

    __tablename__ = "baseline"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"))
    label: Mapped[str] = mapped_column(String(64), default="latest")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Regression(Base):
    """回归结论：pass / block / warn。"""

    __tablename__ = "regression"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    baseline_run_id: Mapped[str] = mapped_column(ForeignKey("run.id"))
    candidate_run_id: Mapped[str] = mapped_column(ForeignKey("run.id"))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    verdict: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Gate(Base):
    """门禁规则。"""

    __tablename__ = "gate"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    scope_tags: Mapped[list[str]] = mapped_column(JSONType, default=list)
    rules: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class JudgeLabel(Base):
    """Judge 校准用的人工标注，必须绑定到具体某次输出。"""

    __tablename__ = "judge_label"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("case.id"), index=True)
    case_result_id: Mapped[str] = mapped_column(ForeignKey("case_result.id"), index=True)
    human_score: Mapped[int] = mapped_column(Integer)
    judge_score: Mapped[int] = mapped_column(Integer)
    rubric_version: Mapped[str] = mapped_column(String(64))
    labeled_by: Mapped[str] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class ToolRecording(Base):
    """工具响应录制，支撑离线回放。"""

    __tablename__ = "tool_recording"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    case_result_id: Mapped[str] = mapped_column(ForeignKey("case_result.id"), index=True)
    step: Mapped[int] = mapped_column(Integer)
    tool: Mapped[str] = mapped_column(String(128))
    request_digest: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(16), default="fixture")


class WebhookEvent(Base):
    """GitHub 重投递幂等去重。"""

    __tablename__ = "webhook_event"

    delivery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event: Mapped[str] = mapped_column(String(64))
    payload_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
