"""adapter 契约：只需把一次执行变成标准轨迹。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from evalops.cases.dsl import CaseSpec


class AdapterError(RuntimeError):
    """adapter 执行失败（fixture 缺失、超时、连接错误等）。"""


@dataclass
class Step:
    step: int
    type: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    tokens: int = 0
    latency_ms: int = 0


@dataclass
class Trajectory:
    output: str
    steps: list[Step] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0

    def tool_calls(self) -> list[Step]:
        return [step for step in self.steps if step.type == "tool"]

    def to_dicts(self) -> list[dict[str, Any]]:
        return [asdict(step) for step in self.steps]


def trajectory_from_dict(data: Mapping[str, Any], latency_fallback: int = 0) -> Trajectory:
    """把标准轨迹 dict 归一化为 ``Trajectory``，供各真实 adapter 复用。"""
    raw_steps = data.get("steps") or []
    if not isinstance(raw_steps, list):
        raise AdapterError("轨迹字段 steps 必须是数组")
    steps: list[Step] = []
    for index, item in enumerate(raw_steps):
        if not isinstance(item, Mapping):
            raise AdapterError(f"轨迹第 {index + 1} 步必须是对象")
        steps.append(
            Step(
                step=int(item.get("step", index + 1)),
                type=str(item.get("type", "llm")),
                name=str(item.get("name", "")),
                input=dict(item.get("input") or {}),
                output=item.get("output"),
                tokens=int(item.get("tokens", 0)),
                latency_ms=int(item.get("latency_ms", 0)),
            )
        )
    return Trajectory(
        output=str(data.get("output") or ""),
        steps=steps,
        tokens_in=int(data.get("tokens_in", 0)),
        tokens_out=int(data.get("tokens_out", 0)),
        latency_ms=int(data.get("latency_ms", latency_fallback)),
    )


class AgentAdapter(Protocol):
    name: str

    async def run(self, case: CaseSpec, config: dict[str, Any]) -> Trajectory: ...
