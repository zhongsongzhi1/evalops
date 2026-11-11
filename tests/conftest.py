"""测试公共夹具：复制 examples 到临时目录，数据库隔离到 tmp_path。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from evalops.config import sqlite_url

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"

# 本机若配置了系统代理（Windows 注册表），httpx 会把 127.0.0.1 也代理出去，
# 本地 HTTP 夹具因此返回 502。测试统一绕过回环地址代理。
os.environ["NO_PROXY"] = ",".join(
    filter(None, [os.environ.get("NO_PROXY"), "127.0.0.1", "localhost"])
)
os.environ["no_proxy"] = os.environ["NO_PROXY"]


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "workspace"
    shutil.copytree(EXAMPLES, target)
    monkeypatch.setenv("EVALOPS_DB_URL", sqlite_url(tmp_path / "evalops.db"))
    return target
