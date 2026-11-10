"""HTTP adapter：把用例 POST 给任意 Agent 服务，契约只要求返回标准轨迹。

请求体::

    {"case_key": "...", "name": "...", "input": {...}, "tags": ["..."]}

响应体（与 fake fixture 相同）::

    {"output": "...", "steps": [...], "tokens_in": 0, "tokens_out": 0, "latency_ms": 0}
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

import httpx

from evalops.adapters.base import AdapterError, Trajectory, trajectory_from_dict
from evalops.cases.dsl import CaseSpec


class HttpAdapter:
    name = "http"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        endpoint = self.config.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
            raise AdapterError("http adapter 需要 config.endpoint（http/https URL）")
        self.endpoint = endpoint
        headers = self.config.get("headers") or {}
        if not isinstance(headers, dict):
            raise AdapterError("config.headers 必须是映射")
        self.headers = {str(key): str(value) for key, value in headers.items()}
        self.timeout_s = float(self.config.get("timeout_s", 30))

    async def run(self, case: CaseSpec, config: dict[str, Any]) -> Trajectory:
        payload = {
            "case_key": case.case_key,
            "name": case.name,
            "input": case.input,
            "tags": list(case.tags),
        }
        started = perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(
                    self.endpoint, json=payload, headers=self.headers
                )
        except httpx.HTTPError as exc:
            raise AdapterError(f"HTTP 请求失败: {exc}") from exc
        if response.status_code >= 400:
            raise AdapterError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise AdapterError(f"HTTP 响应不是 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise AdapterError("HTTP 响应必须是 JSON 对象")
        elapsed = int((perf_counter() - started) * 1000)
        return trajectory_from_dict(data, latency_fallback=elapsed)
