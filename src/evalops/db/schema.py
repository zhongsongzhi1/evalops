"""数据库结构初始化：统一通过 Alembic 迁移保证版本一致。

注意：``ensure_schema`` 内部会自行创建事件循环，必须在事件循环外调用。
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def ensure_schema(url: str) -> None:
    """把数据库升级到最新版本；空库会创建全部表结构。"""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")
