"""Source registry: sources.yaml is the single place URLs live, and its mirror in the DB."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from sentinela.domain import SourceSpec
from sentinela.fetch import canonical_url
from sentinela.models import Source, SourceHealth

REGISTRY_PATH = Path("sources.yaml")
CONFIG_FIELDS = (
    "allowed_hosts",
    "seed_urls",
    "link_pattern",
    "max_documents",
    "max_depth",
    "expected_min_links",
    "validation_url",
)


def load_registry(path: str | Path = REGISTRY_PATH) -> list[SourceSpec]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    specs = [SourceSpec.model_validate(entry) for entry in raw.get("sources", [])]
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            raise ValueError(f"Fonte duplicada no registry: {spec.id}")
        seen.add(spec.id)
        if not canonical_url(spec.base_url):
            raise ValueError(f"base_url invalida em {spec.id}: {spec.base_url}")
        if not spec.allowed_hosts:
            raise ValueError(f"Fonte {spec.id} sem allowed_hosts: coleta seria irrestrita")
    return specs


def save_registry(specs: list[SourceSpec], path: str | Path = REGISTRY_PATH) -> None:
    payload = {
        "sources": [
            {
                key: (value.isoformat() if isinstance(value, datetime) else value)
                for key, value in spec.model_dump().items()
            }
            for spec in sorted(specs, key=lambda item: (item.priority, item.id))
        ]
    }
    Path(path).write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8"
    )


def sync(session: Session, specs: list[SourceSpec]) -> dict[str, int]:
    """Reconcile YAML into the database without destroying runtime health history."""
    existing = {row.id: row for row in session.query(Source).all()}
    created = updated = disabled = 0
    for spec in specs:
        config = {name: getattr(spec, name) for name in CONFIG_FIELDS}
        validated = spec.validated_at
        if isinstance(validated, str):
            try:
                validated = datetime.fromisoformat(validated.replace("Z", "+00:00"))
            except ValueError:
                validated = None
        row = existing.get(spec.id)
        if row is None:
            session.add(
                Source(
                    id=spec.id,
                    name=spec.name,
                    institution=spec.institution,
                    base_url=spec.base_url,
                    official=spec.official,
                    trust_level=spec.trust_level,
                    priority=spec.priority,
                    adapter=spec.adapter,
                    discovery_method=spec.discovery_method,
                    enabled=spec.enabled,
                    trust_status=spec.trust_status,
                    config=config,
                    limitations=spec.limitations,
                    validated_at=validated,
                )
            )
            session.add(SourceHealth(source_id=spec.id))
            created += 1
            continue
        row.name, row.institution, row.base_url = spec.name, spec.institution, spec.base_url
        row.official, row.trust_level, row.priority = spec.official, spec.trust_level, spec.priority
        row.adapter, row.discovery_method = spec.adapter, spec.discovery_method
        row.enabled, row.trust_status = spec.enabled, spec.trust_status
        row.config, row.limitations, row.validated_at = config, spec.limitations, validated
        updated += 1
    known = {spec.id for spec in specs}
    for source_id, row in existing.items():
        # A source removed from YAML is disabled, never deleted: its history stays auditable.
        if source_id not in known and row.enabled:
            row.enabled = False
            disabled += 1
    session.flush()
    for source_id in known:
        if session.get(SourceHealth, source_id) is None:
            session.add(SourceHealth(source_id=source_id))
    session.flush()
    return {"created": created, "updated": updated, "disabled": disabled}


def spec_from_row(row: Source) -> SourceSpec:
    config: dict[str, Any] = dict(row.config or {})
    return SourceSpec(
        id=row.id,
        name=row.name,
        institution=row.institution,
        base_url=row.base_url,
        official=row.official,
        trust_level=row.trust_level,
        priority=row.priority,
        adapter=row.adapter,
        discovery_method=row.discovery_method,
        enabled=row.enabled,
        trust_status=row.trust_status,
        limitations=row.limitations or "",
        allowed_hosts=list(config.get("allowed_hosts") or []),
        seed_urls=list(config.get("seed_urls") or []),
        link_pattern=str(
            config.get("link_pattern") or SourceSpec.model_fields["link_pattern"].default
        ),
        max_documents=int(config.get("max_documents") or 10),
        max_depth=int(config.get("max_depth") or 2),
        expected_min_links=int(config.get("expected_min_links") or 0),
        validation_url=str(config.get("validation_url") or row.base_url),
        validated_at=row.validated_at,
    )


def register_candidate(
    session: Session,
    *,
    source_id: str,
    name: str,
    institution: str,
    base_url: str,
    discovered_from: str,
    allowed_hosts: list[str],
) -> bool:
    """Newly discovered sources enter as CANDIDATE and never feed primary alerts."""
    if session.get(Source, source_id) is not None:
        return False
    session.add(
        Source(
            id=source_id,
            name=name,
            institution=institution,
            base_url=base_url,
            official=False,
            trust_level=3,
            priority=20,
            adapter="generic",
            discovery_method="html",
            enabled=True,
            trust_status="CANDIDATE",
            config={
                "allowed_hosts": allowed_hosts,
                "seed_urls": [],
                "max_documents": 5,
                "max_depth": 1,
                "expected_min_links": 0,
                "validation_url": base_url,
            },
            limitations="Descoberta automatica: aguarda validacao manual antes de virar TRUSTED.",
            discovered_from=discovered_from,
            validated_at=None,
        )
    )
    session.add(SourceHealth(source_id=source_id))
    session.flush()
    return True


def promote(session: Session, source_id: str, trust_level: int = 2) -> bool:
    row = session.get(Source, source_id)
    if row is None or row.trust_status == "TRUSTED":
        return False
    row.trust_status = "TRUSTED"
    row.trust_level = trust_level
    row.validated_at = datetime.now(UTC)
    session.flush()
    return True
