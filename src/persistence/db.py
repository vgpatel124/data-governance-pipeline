from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.config.settings import settings


@lru_cache(maxsize=8)
def _engine_for(db_path: str) -> Engine:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # SQLite is single-writer: background pipeline runs can contend on pipeline.db.
    # WAL + a busy timeout let writers wait instead of raising "database is locked".
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30.0},
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    from src.persistence.models import Base  # local import avoids a cycle
    Base.metadata.create_all(engine)
    return engine


def get_engine(db_path: str | None = None) -> Engine:
    """Engine for pipeline.db (business outcomes). Read settings at call time so
    tests can point DB_PATH at a temp file. Never used for LangGraph checkpoints."""
    return _engine_for(str(Path(db_path or settings.DB_PATH).resolve()))


def get_session(db_path: str | None = None) -> Session:
    return sessionmaker(bind=get_engine(db_path), future=True, expire_on_commit=False)()
