"""Shared database setup. SQLite for local dev (zero setup), any SQLAlchemy URL in production."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event

ROOT = Path(__file__).resolve().parent.parent


def resolve_sqlite_url(url: str) -> str:
    """sqlite:///./twin.db means the repo's twin.db, whichever folder a command is started from
    (otherwise a tool run from elsewhere silently gets a second, empty database)."""
    prefix = "sqlite:///"
    if not url.startswith(prefix) or url == "sqlite://" or url.startswith("sqlite:////"):
        return url
    path = url[len(prefix):]
    if not path or path == ":memory:" or path.startswith(("file:", "/")):
        return url
    return prefix + str((ROOT / path).resolve())


def make_engine(url: str) -> Engine:
    url = resolve_sqlite_url(url)
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_connect(dbapi_conn, _rec):  # pragma: no cover - driver hook
            # Let us control BEGIN ourselves, and use WAL so readers never block writers.
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin(conn):  # pragma: no cover - driver hook
            # IMMEDIATE takes the write lock up front, so concurrent writers queue
            # instead of failing with "database is locked".
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        return engine
    return create_engine(url, pool_pre_ping=True)
