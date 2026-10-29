"""数据库连接。默认 SQLite（本地开发），DATABASE_URL 换成 PostgreSQL 即可切换。"""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def init_engine(url: str) -> Engine:
    global _engine, _session_factory
    is_sqlite = url.startswith("sqlite")
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 15} if is_sqlite else {})
    if is_sqlite:
        # SQLite 没有 SELECT ... FOR UPDATE。每个事务都以 BEGIN IMMEDIATE 开始，
        # 写锁在事务开头就拿到，额度预占因此是串行的（PostgreSQL 上改用行锁）。
        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _record):
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        @event.listens_for(engine, "begin")
        def _on_begin(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    _engine = engine
    _session_factory = sessionmaker(engine, expire_on_commit=False)
    return engine


def get_engine() -> Engine:
    assert _engine is not None, "init_engine() 尚未调用"
    return _engine


def new_session() -> Session:
    assert _session_factory is not None, "init_engine() 尚未调用"
    return _session_factory()


def get_db() -> Iterator[Session]:
    with new_session() as db:
        yield db
