"""adapter 注册表：只认白名单，禁止按 YAML 任意 import 模块。"""

from __future__ import annotations

from evalops.adapters.base import (
    AdapterError,
    AgentAdapter,
    Step,
    Trajectory,
    trajectory_from_dict,
)
from evalops.adapters.entrypoint import EntrypointAdapter
from evalops.adapters.fake import FakeAdapter
from evalops.adapters.http import HttpAdapter
from evalops.cases.dsl import LoadedProject

__all__ = [
    "AdapterError",
    "AgentAdapter",
    "EntrypointAdapter",
    "FakeAdapter",
    "HttpAdapter",
    "Step",
    "Trajectory",
    "build_adapter",
    "trajectory_from_dict",
]


def build_adapter(project: LoadedProject) -> AgentAdapter:
    spec = project.spec
    if spec.adapter == "fake":
        return FakeAdapter(base_dir=project.base_dir, config=spec.config)
    if spec.adapter == "http":
        return HttpAdapter(config=spec.config)
    if spec.adapter in ("langgraph", "openai_agents"):
        if spec.entrypoint is None:
            raise AdapterError(f"adapter={spec.adapter} 缺少 entrypoint")
        return EntrypointAdapter(
            name=spec.adapter, base_dir=project.base_dir, entrypoint=spec.entrypoint
        )
    raise AdapterError(f"未知 adapter: {spec.adapter}")
