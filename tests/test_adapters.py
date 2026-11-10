"""真实 adapter 测试：http 与 entrypoint（langgraph / openai_agents）。"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from evalops.adapters import AdapterError, EntrypointAdapter, HttpAdapter, build_adapter
from evalops.cases.dsl import CaseSpec, LoadedProject, ProjectSpec


def _case() -> CaseSpec:
    return CaseSpec.model_validate(
        {
            "name": "adapter 用例",
            "key": "adapter_case",
            "input": {"question": "你好"},
            "assertions": [{"type": "contains", "values": ["你好"]}],
        }
    )


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/fail":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        body = json.dumps(
            {
                "output": f"echo:{payload.get('case_key')}",
                "tokens_in": 5,
                "tokens_out": 2,
                "steps": [{"type": "tool", "name": "echo", "input": {"q": "你好"}}],
            },
            ensure_ascii=False,
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture()
def http_endpoint() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/run"
    finally:
        server.shutdown()
        server.server_close()


async def test_http_adapter_round_trip(http_endpoint: str) -> None:
    adapter = HttpAdapter({"endpoint": http_endpoint})
    trajectory = await adapter.run(_case(), {})
    assert trajectory.output == "echo:adapter_case"
    assert trajectory.tokens_in == 5
    assert trajectory.tokens_out == 2
    assert trajectory.steps[0].name == "echo"
    assert trajectory.steps[0].type == "tool"


async def test_http_adapter_reports_server_error(http_endpoint: str) -> None:
    adapter = HttpAdapter({"endpoint": http_endpoint.replace("/run", "/fail")})
    with pytest.raises(AdapterError, match="HTTP 500"):
        await adapter.run(_case(), {})


def test_http_adapter_requires_endpoint() -> None:
    with pytest.raises(AdapterError, match="endpoint"):
        HttpAdapter({})


async def test_entrypoint_adapter_sync(tmp_path: Path) -> None:
    (tmp_path / "sut_agent.py").write_text(
        "def run(case, config):\n"
        "    return {'output': case['input']['question'], 'tokens_in': 3,"
        " 'steps': [{'type': 'tool', 'name': 'lookup'}]}\n",
        encoding="utf-8",
    )
    adapter = EntrypointAdapter("langgraph", tmp_path, "sut_agent:run")
    trajectory = await adapter.run(_case(), {})
    assert trajectory.output == "你好"
    assert trajectory.tokens_in == 3
    assert trajectory.steps[0].name == "lookup"


async def test_entrypoint_adapter_async(tmp_path: Path) -> None:
    (tmp_path / "sut_agent.py").write_text(
        "async def run(case, config):\n"
        "    return {'output': 'async:' + case['case_key']}\n",
        encoding="utf-8",
    )
    adapter = EntrypointAdapter("openai_agents", tmp_path, "sut_agent:run")
    trajectory = await adapter.run(_case(), {})
    assert trajectory.output == "async:adapter_case"


def test_entrypoint_rejects_module_outside_project(tmp_path: Path) -> None:
    adapter = EntrypointAdapter("langgraph", tmp_path, "json:loads")
    with pytest.raises(AdapterError, match="项目目录内"):
        adapter._resolve()


def test_entrypoint_requires_valid_contract(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="module:function"):
        EntrypointAdapter("langgraph", tmp_path, "sut_agent")

    (tmp_path / "sut_agent.py").write_text("value = 1\n", encoding="utf-8")
    adapter = EntrypointAdapter("langgraph", tmp_path, "sut_agent:run")
    with pytest.raises(AdapterError, match="找不到可调用对象"):
        adapter._resolve()


def test_build_adapter_wires_http_and_entrypoint(tmp_path: Path) -> None:
    http_spec = ProjectSpec(
        name="demo-http", adapter="http", config={"endpoint": "http://127.0.0.1:1/run"}
    )
    http_adapter = build_adapter(
        LoadedProject(spec=http_spec, path=tmp_path / "project.yaml")
    )
    assert isinstance(http_adapter, HttpAdapter)

    (tmp_path / "sut_agent.py").write_text(
        "def run(case, config):\n    return {'output': 'ok'}\n", encoding="utf-8"
    )
    graph_spec = ProjectSpec(
        name="demo-graph", adapter="langgraph", entrypoint="sut_agent:run"
    )
    graph_adapter = build_adapter(
        LoadedProject(spec=graph_spec, path=tmp_path / "project.yaml")
    )
    assert isinstance(graph_adapter, EntrypointAdapter)
