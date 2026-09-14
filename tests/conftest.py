from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from sentinela.config import Configuration, Secrets
from sentinela.db import create_engine, session_factory
from sentinela.models import Base, Source, SourceHealth

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 9, 14)


def validate_test_database_url(value: str) -> str:
    """Reject a remote/production database before destructive test fixtures connect."""
    try:
        url = sa.engine.make_url(value)
    except (sa.exc.ArgumentError, ValueError):
        raise pytest.UsageError("TEST_DATABASE_URL is not a valid test database URL") from None
    if url.get_backend_name() == "sqlite":
        return value
    # Fixtures drop every application table. Never allow Supabase or any remote host,
    # even when a production connection string was accidentally copied into TEST_*.
    # Query parameters can override libpq host/dbname, so none are accepted here.
    if (
        url.get_backend_name() != "postgresql"
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or not url.database
        or not url.database.endswith("_test")
        or url.query
    ):
        raise pytest.UsageError(
            "TEST_DATABASE_URL must use SQLite or a disposable loopback PostgreSQL "
            "database ending in '_test', without URL query parameters; fixtures drop tables"
        )
    return value


@pytest.fixture(scope="session")
def database_url() -> str:
    """PostgreSQL when TEST_DATABASE_URL is set, SQLite otherwise.

    The schema is dialect-portable on purpose so the fast unit suite needs no server,
    while CI still exercises the real database the system runs on.
    """
    value = os.environ.get("TEST_DATABASE_URL") or "sqlite+pysqlite:///:memory:"
    return validate_test_database_url(value)


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    instance = create_engine(database_url)
    if instance.dialect.name != "sqlite":
        Base.metadata.drop_all(instance)
    Base.metadata.create_all(instance)
    yield instance
    if instance.dialect.name != "sqlite":
        Base.metadata.drop_all(instance)
    instance.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = session_factory(engine)
    with factory() as active:
        yield active
        active.rollback()


@pytest.fixture
def config() -> Configuration:
    return Configuration(
        notifications={"telegram": False, "email": False, "console": False, "markdown": True},
        storage={"reports_directory": "reports"},
    )


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(_env_file=None)


@pytest.fixture
def source(session: Session) -> Source:
    row = Source(
        id="fonte-teste",
        name="Fonte de teste",
        institution="Prefeitura Municipal de Rio Branco",
        base_url="https://exemplo.riobranco.ac.gov.br/editais",
        official=True,
        trust_level=1,
        priority=1,
        adapter="generic",
        discovery_method="html",
        enabled=True,
        trust_status="TRUSTED",
        config={
            "allowed_hosts": ["exemplo.riobranco.ac.gov.br"],
            "seed_urls": [],
            "max_documents": 5,
            "max_depth": 1,
            "expected_min_links": 0,
            "validation_url": "https://exemplo.riobranco.ac.gov.br/editais",
        },
        limitations="",
    )
    session.add(row)
    session.add(SourceHealth(source_id=row.id))
    session.commit()
    return row


@pytest.fixture
def camara_pdf() -> bytes:
    path = FIXTURES / "public_sources" / "camara_rio_branco_2026.pdf"
    if not path.is_file():
        pytest.skip("fixture de edital real ausente")
    return path.read_bytes()


@pytest.fixture
def frozen_now() -> datetime:
    return datetime(2026, 9, 14, 10, 17, tzinfo=UTC)


def count(session: Session, model: type[Base]) -> int:
    return session.execute(sa.select(sa.func.count()).select_from(model)).scalar_one()
