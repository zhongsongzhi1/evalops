"""entrypoint adapter：给 langgraph / openai_agents 项目提供一个薄接入方式。

约定 ``entrypoint`` 形如 ``module:function``，且模块必须位于项目目录内；
函数签名为 ``run(case, config)``，可以同步或异步，返回标准轨迹 dict 或 Trajectory::

    def run(case, config):
        graph = build_graph()
        state = graph.invoke({"messages": case["input"]["messages"]})
        return {"output": state["messages"][-1].content, "steps": [...]}

框架相关的状态拼装留在项目侧（≤50 行），平台只负责执行与归一化轨迹。
"""

from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from evalops.adapters.base import AdapterError, Trajectory, trajectory_from_dict
from evalops.cases.dsl import CaseSpec


@contextmanager
def _project_import_path(base_dir: Path) -> Iterator[None]:
    value = str(base_dir)
    sys.path.insert(0, value)
    importlib.invalidate_caches()
    try:
        yield
    finally:
        try:
            sys.path.remove(value)
        except ValueError:  # pragma: no cover - 仅防御性处理
            pass


class EntrypointAdapter:
    def __init__(self, name: str, base_dir: Path, entrypoint: str) -> None:
        module_name, separator, attribute = entrypoint.partition(":")
        if not module_name or not separator or not attribute:
            raise AdapterError(f"entrypoint 必须形如 module:function，实际为 {entrypoint!r}")
        self.name = name
        self.base_dir = Path(base_dir).resolve()
        self.entrypoint = entrypoint
        self._module_name = module_name
        self._attribute = attribute
        self._function: Any = None

    def _resolve(self) -> Any:
        if self._function is not None:
            return self._function

        cached = sys.modules.get(self._module_name)
        if cached is not None and not self._inside_project(cached):
            del sys.modules[self._module_name]

        with _project_import_path(self.base_dir):
            try:
                module = importlib.import_module(self._module_name)
            except Exception as exc:  # noqa: BLE001 - 项目代码的导入错误统一转成 adapter 错误
                raise AdapterError(f"加载 {self._module_name} 失败: {exc}") from exc

        if not self._inside_project(module):
            raise AdapterError(
                f"entrypoint 只允许加载项目目录内的模块: {self._module_name}"
            )
        function = getattr(module, self._attribute, None)
        if not callable(function):
            raise AdapterError(
                f"{self._module_name} 中找不到可调用对象 {self._attribute}"
            )
        self._function = function
        return function

    def _inside_project(self, module: Any) -> bool:
        module_file = getattr(module, "__file__", None)
        if not module_file:
            return False
        return Path(module_file).resolve().is_relative_to(self.base_dir)

    async def run(self, case: CaseSpec, config: dict[str, Any]) -> Trajectory:
        function = self._resolve()
        payload = {
            "case_key": case.case_key,
            "name": case.name,
            "input": case.input,
            "tags": list(case.tags),
        }
        try:
            result = function(payload, config)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - 被测代码异常统一记为 adapter 错误
            raise AdapterError(f"entrypoint 执行失败: {exc}") from exc

        if isinstance(result, Trajectory):
            return result
        if isinstance(result, Mapping):
            return trajectory_from_dict(result)
        raise AdapterError(
            f"entrypoint 返回值必须是 Trajectory 或 dict，实际是 {type(result)!r}"
        )
