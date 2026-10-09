"""SQLite engine and short lived sessions for UI and background operations."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import ensure_local_directories, settings
from app.models import Base


def _create_engine(database_path: str | Path) -> Engine:
    filename = str(database_path)
    if filename != ":memory:":
        path = Path(database_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        filename = str(path)
    db_engine = create_engine(
        URL.create("sqlite", database=filename),
        connect_args={"check_same_thread": False, "timeout": 30},
        **({"poolclass": StaticPool} if filename == ":memory:" else {}),
    )

    @event.listens_for(db_engine, "connect")
    def _configure_sqlite(connection, connection_record) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    return db_engine


engine = _create_engine(settings.database_path)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def init_db(bind: Engine | None = None) -> None:
    if bind is None:
        ensure_local_directories()
    Base.metadata.create_all(bind or engine)


def create_session_factory(database_path: str | Path) -> sessionmaker[Session]:
    """Create an isolated initialized database for tests or alternate storage."""
    db_engine = _create_engine(database_path)
    init_db(db_engine)
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)
