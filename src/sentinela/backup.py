"""Database backup, verification and restore helpers.

The backup is a logical dump produced by the database itself when `pg_dump` is available,
and a portable JSON export otherwise, so a GitHub Actions runner without PostgreSQL client
tools still produces something restorable.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as dt_date
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, make_url

from sentinela.domain import digest
from sentinela.models import Base

RETENTION_DAYS = 30
EXPORT_ORDER = (
    "sources",
    "source_health",
    "monitor_runs",
    "source_runs",
    "documents",
    "document_versions",
    "raw_snapshots",
    "opportunities",
    "positions",
    "opportunity_versions",
    "deadlines",
    "notifications",
    "classification_history",
    "extraction_results",
    "errors",
    "system_events",
)


@dataclass(slots=True)
class BackupResult:
    path: Path
    method: str  # pg_dump | json
    rows: int
    bytes: int
    sha256: str
    created_at: datetime


def _pg_dump_available() -> str | None:
    return shutil.which("pg_dump")


def _dsn(url: str) -> str:
    """SQLAlchemy URL -> libpq URL that pg_dump understands."""
    parsed = make_url(url)
    return str(parsed.set(drivername="postgresql")).replace("%40", "@")


def dump(
    engine: Engine, directory: str | Path = "backups", stamp: datetime | None = None
) -> BackupResult:
    moment = stamp or datetime.now(UTC)
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    name = f"sentinela-{moment:%Y%m%dT%H%M%SZ}"

    binary = _pg_dump_available()
    if binary and engine.dialect.name == "postgresql":
        path = target / f"{name}.dump.gz"
        environment = dict(os.environ)
        # The password travels in the DSN, never on the command line.
        process = subprocess.run(  # noqa: S603 - fixed binary, no shell
            [
                binary,
                "--format=plain",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                _dsn(str(engine.url.render_as_string(hide_password=False))),
            ],
            capture_output=True,
            check=False,
            env=environment,
            timeout=900,
        )
        if process.returncode == 0 and process.stdout:
            with gzip.open(path, "wb") as stream:
                stream.write(process.stdout)
            content = path.read_bytes()
            return BackupResult(
                path, "pg_dump", process.stdout.count(b"\n"), len(content), digest(content), moment
            )

    path = target / f"{name}.json.gz"
    payload: dict[str, Any] = {
        "schema": "sentinela-ac/1",
        "created_at": moment.isoformat(),
        "tables": {},
    }
    rows = 0
    with engine.connect() as connection:
        for table_name in EXPORT_ORDER:
            table = Base.metadata.tables.get(table_name)
            if table is None:
                continue
            records = []
            for row in connection.execute(sa.select(table)).mappings():
                records.append(
                    {
                        key: (
                            value.isoformat()
                            if isinstance(value, datetime)
                            else float(value)
                            if hasattr(value, "quantize")
                            else value.isoformat()
                            if hasattr(value, "year") and not isinstance(value, str)
                            else value
                        )
                        for key, value in row.items()
                    }
                )
            payload["tables"][table_name] = records
            rows += len(records)
    body = json.dumps(payload, ensure_ascii=False, default=str).encode()
    with gzip.open(path, "wb") as stream:
        stream.write(body)
    content = path.read_bytes()
    return BackupResult(path, "json", rows, len(content), digest(content), moment)


def verify(path: str | Path) -> tuple[bool, str]:
    """A backup nobody can read is not a backup. This is what the workflow asserts."""
    target = Path(path)
    if not target.is_file() or target.stat().st_size < 64:
        return False, "Arquivo ausente ou vazio"
    try:
        with gzip.open(target, "rb") as stream:
            head = stream.read(4096)
            total = len(head)
            while chunk := stream.read(1 << 20):
                total += len(chunk)
    except OSError as error:
        return False, f"Arquivo corrompido: {type(error).__name__}"
    if target.name.endswith(".json.gz"):
        try:
            with gzip.open(target, "rb") as stream:
                payload = json.load(stream)
            tables = payload.get("tables") or {}
        except (OSError, ValueError) as error:
            return False, f"JSON invalido: {type(error).__name__}"
        if "opportunities" not in tables:
            return False, "Export sem a tabela opportunities"
        return True, f"{sum(len(rows) for rows in tables.values())} registros, {total} bytes"
    if b"PostgreSQL database dump" not in head and b"CREATE TABLE" not in head:
        return False, "Dump sem cabecalho reconhecivel"
    return True, f"{total} bytes descompactados"


def prune(directory: str | Path = "backups", keep_days: int = RETENTION_DAYS) -> list[Path]:
    target = Path(directory)
    if not target.is_dir():
        return []
    cutoff = datetime.now(UTC).timestamp() - keep_days * 86400
    removed: list[Path] = []
    for path in sorted(target.glob("sentinela-*.gz")):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def restore_json(engine: Engine, path: str | Path) -> dict[str, int]:
    """Restore a JSON export into an empty schema. Refuses to run over existing data."""
    from sentinela.models import Opportunity

    with engine.connect() as connection:
        if sa.inspect(engine).has_table("opportunities"):
            count = connection.execute(
                sa.select(sa.func.count()).select_from(Opportunity.__table__)
            ).scalar_one()
            if count:
                raise RuntimeError(
                    "Banco de destino nao esta vazio: restaure em um banco limpo para nao "
                    "sobrescrever evidencias existentes."
                )
    with gzip.open(Path(path), "rb") as stream:
        payload = json.load(stream)
    inserted: dict[str, int] = {}
    with engine.begin() as connection:
        for table_name in EXPORT_ORDER:
            rows = (payload.get("tables") or {}).get(table_name) or []
            table = Base.metadata.tables.get(table_name)
            if table is None or not rows:
                continue
            connection.execute(sa.insert(table), [_coerce(table, row) for row in rows])
            inserted[table_name] = len(rows)
    return inserted


def _coerce(table: sa.Table, row: dict[str, Any]) -> dict[str, Any]:
    """Turn JSON scalars back into the Python types the columns expect."""
    restored: dict[str, Any] = {}
    for name, value in row.items():
        column = table.columns.get(name)
        if column is None or value is None:
            restored[name] = value
            continue
        python_type: Any
        try:
            python_type = column.type.python_type
        except NotImplementedError:
            python_type = None
        if python_type is datetime and isinstance(value, str):
            restored[name] = datetime.fromisoformat(value)
        elif python_type is dt_date and isinstance(value, str):
            restored[name] = dt_date.fromisoformat(value[:10])
        elif python_type is Decimal and isinstance(value, int | float | str):
            restored[name] = Decimal(str(value))
        else:
            restored[name] = value
    return restored
