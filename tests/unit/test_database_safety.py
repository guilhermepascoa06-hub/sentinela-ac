"""No database connection is needed to verify the destructive fixture's guard."""

import pytest

from tests.conftest import validate_test_database_url


@pytest.mark.parametrize(
    "value",
    [
        "sqlite+pysqlite:///:memory:",
        "postgresql+psycopg://sentinela@localhost:5432/sentinela_test",
        "postgresql+psycopg://sentinela@127.0.0.1:55439/sentinela_test",
        "postgresql+psycopg://sentinela@[::1]:55439/sentinela_test",
    ],
)
def test_disposable_local_database_is_accepted(value: str) -> None:
    assert validate_test_database_url(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "postgresql+psycopg://sentinela@database.example.com/sentinela_test",
        "postgresql+psycopg://sentinela@localhost:5432/postgres",
        "postgresql+psycopg://sentinela@localhost:5432/sentinela",
        "postgresql+psycopg://sentinela@localhost:5432/",
        "postgresql+psycopg://sentinela@localhost/sentinela_test?host=database.example.com",
        "postgresql+psycopg://sentinela@localhost/sentinela_test?dbname=postgres",
        "postgresql+psycopg:///sentinela_test",
        "mysql://sentinela@localhost/sentinela_test",
        "not-a-database-url",
    ],
)
def test_unsafe_database_is_rejected_before_connecting(value: str) -> None:
    with pytest.raises(pytest.UsageError, match="TEST_DATABASE_URL"):
        validate_test_database_url(value)


def test_invalid_connection_details_are_not_echoed() -> None:
    private_value = "this-is-private-and-is-not-a-url"
    with pytest.raises(pytest.UsageError) as error:
        validate_test_database_url(private_value)
    assert private_value not in str(error.value)
