"""数据库会话管理。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@dataclass
class Database:
    """一个数据库地址对应的引擎与会话工厂。"""

    url: str
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]

    @classmethod
    def create(cls, url: str) -> Database:
        engine = create_async_engine(url, future=True)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        return cls(url=url, engine=engine, sessionmaker=sessionmaker)

    async def dispose(self) -> None:
        await self.engine.dispose()
