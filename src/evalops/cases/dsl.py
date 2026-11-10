"""project.yaml / cases/*.yaml 的声明式 DSL 与校验规则。

设计要点（见设计文档 §4、§17、§19.2）：
- 用例是数据不是代码，不允许在 YAML 里写任意脚本；
- ``case_key`` 是跨版本稳定身份，默认取 ``name``，可显式覆盖；
- 同一用例集内 ``case_key`` 必须唯一。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ADAPTER_WHITELIST = ("fake", "http", "openai_agents", "langgraph")
CASE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class DslError(ValueError):
    """项目或用例 YAML 校验失败。"""


class ProjectSpec(BaseModel):
    """被测 Agent 的接入声明。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    adapter: Literal["fake", "http", "openai_agents", "langgraph"]
    entrypoint: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name 不能为空")
        return value

    @model_validator(mode="after")
    def _check_entrypoint(self) -> ProjectSpec:
        if self.adapter in ("langgraph", "openai_agents") and not self.entrypoint:
            raise ValueError(f"adapter={self.adapter} 时必须声明 entrypoint")
        if self.adapter in ("fake", "http") and self.entrypoint:
            raise ValueError(f"adapter={self.adapter} 不接受 entrypoint")
        if self.adapter == "http":
            endpoint = self.config.get("endpoint")
            if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
                raise ValueError("adapter=http 时 config.endpoint 必须是 http(s) URL")
        return self


class ContainsAssertion(BaseModel):
    type: Literal["contains"] = "contains"
    values: list[str]

    @field_validator("values")
    @classmethod
    def _not_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("values 不能为空")
        return value


class NotContainsAssertion(BaseModel):
    type: Literal["not_contains"] = "not_contains"
    values: list[str]

    @field_validator("values")
    @classmethod
    def _not_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("values 不能为空")
        return value


class EqualsAssertion(BaseModel):
    type: Literal["equals"] = "equals"
    value: str


class RegexAssertion(BaseModel):
    type: Literal["regex"] = "regex"
    pattern: str

    @field_validator("pattern")
    @classmethod
    def _valid_pattern(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:  # pragma: no cover - 仅在非法正则时触发
            raise ValueError(f"非法正则: {exc}") from exc
        return value


class ToolCallExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    must_call: bool = True
    args_contains: dict[str, Any] = Field(default_factory=dict)


class ToolCallsAssertion(BaseModel):
    type: Literal["tool_calls"] = "tool_calls"
    tools: list[ToolCallExpectation]
    ordered: bool = False

    @field_validator("tools")
    @classmethod
    def _not_empty(cls, value: list[ToolCallExpectation]) -> list[ToolCallExpectation]:
        if not value:
            raise ValueError("tools 不能为空")
        return value


class MaxLatencyAssertion(BaseModel):
    type: Literal["max_latency_ms"] = "max_latency_ms"
    value: int = Field(gt=0)


class MaxCostAssertion(BaseModel):
    type: Literal["max_cost_usd"] = "max_cost_usd"
    value: float = Field(gt=0)


class SemanticAssertion(BaseModel):
    """语义相似度断言：输出与参考表述的余弦相似度不低于 ``threshold``。

    embedding 来源由项目配置 ``config.embedding`` 决定，默认 ``mock``（离线演示）。
    """

    type: Literal["semantic"] = "semantic"
    reference: str
    threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    @field_validator("reference")
    @classmethod
    def _reference_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reference 不能为空")
        return value


class LlmJudgeAssertion(BaseModel):
    """LLM Judge 断言：按 rubric 对输出打分，多次采样取中位数。

    ``rubric`` 指向 Markdown 文件（可带 YAML front matter），路径先按项目目录
    解析，其次用例目录与当前工作目录。评分来源由 ``config.judge`` 声明，
    默认离线 ``mock``（仅用于演示与 CI，不代表真实评审能力）。
    """

    type: Literal["llm_judge"] = "llm_judge"
    rubric: str
    pass_score: float = Field(default=4.0, gt=0.0, le=5.0)
    samples: int = Field(default=3, ge=1, le=9)

    @field_validator("rubric")
    @classmethod
    def _rubric_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rubric 不能为空")
        return value


AssertionSpec = Annotated[
    ContainsAssertion
    | NotContainsAssertion
    | EqualsAssertion
    | RegexAssertion
    | ToolCallsAssertion
    | MaxLatencyAssertion
    | MaxCostAssertion
    | SemanticAssertion
    | LlmJudgeAssertion,
    Field(discriminator="type"),
]


class ExpectedSpec(BaseModel):
    """``expected`` 是对常用断言的简写，编译时排在显式 ``assertions`` 之前。"""

    model_config = ConfigDict(extra="forbid")

    answer_contains: list[str] = Field(default_factory=list)
    answer_not_contains: list[str] = Field(default_factory=list)
    answer_equals: str | None = None
    regex: str | None = None
    tool_calls: list[ToolCallExpectation] = Field(default_factory=list)
    json_schema: dict[str, Any] | None = None


class CaseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    key: str | None = None
    input: dict[str, Any]
    expected: ExpectedSpec = Field(default_factory=ExpectedSpec)
    assertions: list[AssertionSpec] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True

    @property
    def case_key(self) -> str:
        return self.key or self.name

    @model_validator(mode="after")
    def _validate(self) -> CaseSpec:
        if not self.name.strip():
            raise ValueError("name 不能为空")
        if self.key is not None and not CASE_KEY_PATTERN.match(self.key):
            raise ValueError("key 只能包含字母、数字、点、下划线和短横线，长度 1-64")
        if self.expected.json_schema is not None:
            raise ValueError("json_schema 断言暂不支持")
        if not self.compiled_assertions():
            raise ValueError("用例必须至少包含一条断言（expected 简写或 assertions）")
        return self

    def compiled_assertions(self) -> list[AssertionSpec]:
        """把 ``expected`` 简写与显式 ``assertions`` 合并成有序断言列表。"""
        expected = self.expected
        compiled: list[AssertionSpec] = []
        if expected.answer_contains:
            compiled.append(ContainsAssertion(values=expected.answer_contains))
        if expected.answer_not_contains:
            compiled.append(NotContainsAssertion(values=expected.answer_not_contains))
        if expected.answer_equals is not None:
            compiled.append(EqualsAssertion(value=expected.answer_equals))
        if expected.regex is not None:
            compiled.append(RegexAssertion(pattern=expected.regex))
        if expected.tool_calls:
            compiled.append(ToolCallsAssertion(tools=expected.tool_calls))
        compiled.extend(self.assertions)
        return compiled


@dataclass
class LoadedProject:
    spec: ProjectSpec
    path: Path

    @property
    def base_dir(self) -> Path:
        return self.path.parent


@dataclass
class LoadedCaseSet:
    cases: list[CaseSpec]
    content_hash: str


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DslError(f"{path}: YAML 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise DslError(f"{path}: 顶层必须是映射（mapping）")
    return data


def load_project(path: str | Path) -> LoadedProject:
    file_path = Path(path)
    if not file_path.is_file():
        raise DslError(f"项目文件不存在: {file_path}")
    try:
        spec = ProjectSpec.model_validate(_read_yaml(file_path))
    except ValidationError as exc:
        raise DslError(f"{file_path}: 校验失败\n{exc}") from exc
    return LoadedProject(spec=spec, path=file_path)


def iter_case_files(path: str | Path) -> list[Path]:
    target = Path(path)
    if target.is_file():
        return [target]
    if not target.is_dir():
        raise DslError(f"用例路径不存在: {target}")
    files = sorted([*target.glob("*.yaml"), *target.glob("*.yml")])
    if not files:
        raise DslError(f"{target} 下没有 YAML 用例文件")
    return files


def load_cases(path: str | Path) -> LoadedCaseSet:
    cases: list[CaseSpec] = []
    for file_path in iter_case_files(path):
        try:
            cases.append(CaseSpec.model_validate(_read_yaml(file_path)))
        except ValidationError as exc:
            raise DslError(f"{file_path}: 校验失败\n{exc}") from exc

    keys = [case.case_key for case in cases]
    duplicated = sorted({key for key in keys if keys.count(key) > 1})
    if duplicated:
        raise DslError(f"case_key 重复，回归无法对齐: {', '.join(duplicated)}")

    canonical = json.dumps(
        [case.model_dump(mode="json") for case in sorted(cases, key=lambda item: item.case_key)],
        ensure_ascii=False,
        sort_keys=True,
    )
    return LoadedCaseSet(cases=cases, content_hash=hashlib.sha256(canonical.encode()).hexdigest())


def filter_cases(cases: list[CaseSpec], tags: list[str] | None = None) -> list[CaseSpec]:
    """按标签过滤：命中任一标签即执行；不传标签则保留全部启用的用例。"""
    selected = [case for case in cases if case.enabled]
    if tags:
        wanted = set(tags)
        selected = [case for case in selected if wanted & set(case.tags)]
    return selected
