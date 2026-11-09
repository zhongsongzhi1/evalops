"""测试公共夹具：复制 examples 到临时目录，数据库隔离到 tmp_path。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from evalops.config import sqlite_url

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "workspace"
    shutil.copytree(EXAMPLES, target)
    monkeypatch.setenv("EVALOPS_DB_URL", sqlite_url(tmp_path / "evalops.db"))
    return target
