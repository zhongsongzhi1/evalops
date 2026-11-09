"""adapter 契约：只需把一次执行变成标准轨迹。"""

from __future__ import annotations

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


class AgentAdapter(Protocol):
    name: str

    async def run(self, case: CaseSpec, config: dict[str, Any]) -> Trajectory: ...
