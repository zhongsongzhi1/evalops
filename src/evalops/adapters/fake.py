"""fake adapter：读取录制好的轨迹文件，零密钥复现完整评测链路。

fixture 命名规则：``<fixtures_dir>/<case_key>.json``，结构::

    {"output": "...", "tokens_in": 320, "tokens_out": 86, "latency_ms": 1240,
     "steps": [{"step": 1, "type": "tool", "name": "query_order",
                "input": {"order_id": "A1B2C3"}, "output": {...}}]}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evalops.adapters.base import AdapterError, Trajectory, trajectory_from_dict
from evalops.cases.dsl import CaseSpec


class FakeAdapter:
    name = "fake"

    def __init__(self, base_dir: Path, config: dict[str, Any] | None = None) -> None:
        self.base_dir = base_dir
        self.config = config or {}

    @property
    def fixtures_dir(self) -> Path:
        raw = Path(str(self.config.get("fixtures_dir", "fixtures")))
        return raw if raw.is_absolute() else self.base_dir / raw

    async def run(self, case: CaseSpec, config: dict[str, Any]) -> Trajectory:
        path = self.fixtures_dir / f"{case.case_key}.json"
        if not path.is_file():
            raise AdapterError(f"缺少录制文件 {path}；fake adapter 需要为每条用例提供 fixture")
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AdapterError(f"{path}: fixture 不是合法 JSON: {exc}") from exc
        return trajectory_from_dict(data)
