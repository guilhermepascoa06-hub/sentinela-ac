"""Engine, sessions and the distributed lock that keeps runs mutually exclusive."""

from __future__ import annotations

import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from sentinela.config import Secrets

# Advisory lock namespace. Any other application using the same database would need
# to collide on both halves of the key to interfere with us.
LOCK_NAMESPACE = 0x53454E54  # "SENT"


class DatabaseUnavailable(RuntimeError):
    """Raised instead of crashing so the caller can record the failure and exit cleanly."""


def resolve_url(secrets: Secrets | None = None, override: str | None = None) -> str:
    url = override or (secrets or Secrets()).database_url.get_secret_value()
    if not url:
        raise DatabaseUnavailable(
            "DATABASE_URL nao configurada. Copie .env.example para .env e preencha a conexao."
        )
    # Transaction pooling (6543) breaks advisory locks and prepared statements.
    parsed = make_url(url)
    if parsed.port == 6543:
        raise DatabaseUnavailable(
            "Porta 6543 e o pooler de transacao do Supabase: locks consultivos nao sobrevivem. "
            "Use a conexao direta ou o pooler de sessao na porta 5432."
        )
    return url


def create_engine(url: str, **kwargs: Any) -> Engine:
    options: dict[str, Any] = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        # Tests only. StaticPool keeps an in-memory database alive across sessions.
        options["connect_args"] = {"check_same_thread": False}
    else:
        options["connect_args"] = {"connect_timeout": 15, "application_name": "sentinela-ac"}
        # NullPool (used by Alembic) rejects sizing arguments, so only add them by default.
        if "poolclass" not in kwargs:
            options |= {"pool_size": 5, "max_overflow": 5, "pool_recycle": 900}
    options |= kwargs
    engine = sa.create_engine(url, **options)
    if url.startswith("sqlite"):

        @sa.event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _lock_key(name: str) -> int:
    # crc32 keeps the key stable across processes and Python versions, unlike hash().
    return zlib.crc32(name.encode()) & 0x7FFFFFFF


@contextmanager
def advisory_lock(engine: Engine, name: str = "monitor") -> Iterator[bool]:
    """Yield True when this process owns the lock.

    On PostgreSQL this is a session-scoped advisory lock: if the process dies the lock is
    released by the server, so a crashed run can never wedge the schedule. SQLite has no
    equivalent and no concurrency to protect against, so it always grants the lock.
    """
    if engine.dialect.name != "postgresql":
        yield True
        return
    connection = engine.connect()
    acquired = False
    try:
        acquired = bool(
            connection.execute(
                sa.text("SELECT pg_try_advisory_lock(:ns, :key)"),
                {"ns": LOCK_NAMESPACE, "key": _lock_key(name)},
            ).scalar()
        )
        yield acquired
    finally:
        if acquired:
            connection.execute(
                sa.text("SELECT pg_advisory_unlock(:ns, :key)"),
                {"ns": LOCK_NAMESPACE, "key": _lock_key(name)},
            )
            connection.commit()
        connection.close()


def ping(engine: Engine) -> tuple[bool, str]:
    try:
        with engine.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
            version = connection.execute(sa.text("SELECT version()")).scalar() or ""
        return True, str(version).split(",")[0][:80]
    except sa.exc.SQLAlchemyError as error:
        return False, type(error).__name__


def migration_state(engine: Engine) -> tuple[bool, str]:
    """Report the applied Alembic revision without importing Alembic at runtime."""
    try:
        with engine.connect() as connection:
            if not sa.inspect(engine).has_table("alembic_version"):
                return False, "nenhuma migration aplicada"
            revision = connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar()
        return bool(revision), str(revision or "desconhecida")
    except sa.exc.SQLAlchemyError as error:
        return False, type(error).__name__
