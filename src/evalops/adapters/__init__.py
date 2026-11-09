"""adapter 注册表：只认白名单，禁止按 YAML 任意 import 模块。"""

from __future__ import annotations

from evalops.adapters.base import AdapterError, AgentAdapter, Step, Trajectory
from evalops.adapters.fake import FakeAdapter
from evalops.cases.dsl import LoadedProject

__all__ = ["AdapterError", "AgentAdapter", "FakeAdapter", "Step", "Trajectory", "build_adapter"]


def build_adapter(project: LoadedProject) -> AgentAdapter:
    spec = project.spec
    if spec.adapter == "fake":
        return FakeAdapter(base_dir=project.base_dir, config=spec.config)
    raise AdapterError(
        f"adapter={spec.adapter} 在阶段 0 尚未实现；"
        "当前可用：fake（白名单：fake/http/openai_agents/langgraph）"
    )
