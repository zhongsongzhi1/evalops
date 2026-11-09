"""运行配置：数据库地址解析。

默认使用当前目录下的 SQLite 文件，保证零外部依赖、零密钥即可跑通；
生产环境可通过 ``EVALOPS_DB_URL`` 指向 PostgreSQL，例如
``postgresql+asyncpg://user:pass@localhost:5432/evalops``。
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_DB_URL = "EVALOPS_DB_URL"
DEFAULT_DB_FILE = "evalops.db"


def sqlite_url(path: str | Path) -> str:
    """把本地文件路径转换成 SQLite 异步 URL（兼容 Windows / POSIX 绝对路径）。"""
    return "sqlite+aiosqlite:///" + Path(path).as_posix()


def resolve_db_url(db: str | None = None) -> str:
    """解析数据库地址。

    优先级：命令行参数 > 环境变量 > 默认 SQLite 文件。
    ``db`` 既可以是完整 URL，也可以是文件路径。
    """
    raw = db or os.environ.get(ENV_DB_URL) or DEFAULT_DB_FILE
    if "://" in raw:
        return raw
    return sqlite_url(raw)
