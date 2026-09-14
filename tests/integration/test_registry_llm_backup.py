from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from sentinela import backup as backup_module
from sentinela import report as report_module
from sentinela.audit import ISSUE_MARKER, issue_body, report_degraded_sources
from sentinela.config import Configuration, Secrets
from sentinela.domain import SourceSpec, now
from sentinela.llm import (
    LLMResult,
    NullProvider,
    build_provider,
    cache_key,
    extract_semantic,
    fill_gaps,
    load_prompt,
    parse_payload,
)
from sentinela.models import Opportunity, Source, SourceHealth
from sentinela.registry import load_registry, promote, register_candidate, spec_from_row, sync

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- registry


def test_the_shipped_registry_is_valid() -> None:
    specs = load_registry(REPO_ROOT / "sources.yaml")
    assert len(specs) >= 30
    assert all(spec.allowed_hosts for spec in specs)
    assert {spec.id for spec in specs}.__len__() == len(specs)
    official = [spec for spec in specs if spec.official]
    assert len(official) >= 25
    aggregators = [spec for spec in specs if spec.trust_level >= 3]
    assert aggregators, "é preciso ao menos uma fonte de descoberta"
    assert all(not spec.official for spec in aggregators)


def test_registry_rejects_a_source_without_an_allow_list(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n  - id: x\n    name: X\n    institution: X\n"
        "    base_url: https://x.gov.br/\n    allowed_hosts: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="allowed_hosts"):
        load_registry(path)


def test_sync_is_idempotent_and_preserves_health(session: Session) -> None:
    specs = load_registry(REPO_ROOT / "sources.yaml")
    first = sync(session, specs)
    session.commit()
    assert first["created"] == len(specs)

    state = session.get(SourceHealth, specs[0].id)
    assert state is not None
    state.consecutive_failures = 4
    state.state = "DEGRADED"
    session.commit()

    second = sync(session, specs)
    session.commit()
    assert second["created"] == 0
    assert second["updated"] == len(specs)
    assert session.get(SourceHealth, specs[0].id).consecutive_failures == 4


def test_removing_a_source_disables_it_instead_of_deleting_history(session: Session) -> None:
    specs = load_registry(REPO_ROOT / "sources.yaml")
    sync(session, specs)
    session.commit()
    sync(session, specs[:-1])
    session.commit()
    dropped = session.get(Source, specs[-1].id)
    assert dropped is not None and dropped.enabled is False


def test_discovered_sources_start_as_candidates(session: Session) -> None:
    assert (
        register_candidate(
            session,
            source_id="cand-x",
            name="Candidata",
            institution="x.gov.br",
            base_url="https://x.gov.br/",
            discovered_from="tjac",
            allowed_hosts=["x.gov.br"],
        )
        is True
    )
    session.commit()
    row = session.get(Source, "cand-x")
    assert row.trust_status == "CANDIDATE"
    assert row.official is False
    assert row.trust_level == 3  # cannot raise a primary alert
    assert (
        register_candidate(
            session,
            source_id="cand-x",
            name="Outra",
            institution="x",
            base_url="https://x.gov.br/",
            discovered_from="tjac",
            allowed_hosts=["x.gov.br"],
        )
        is False
    )

    assert promote(session, "cand-x") is True
    session.commit()
    assert session.get(Source, "cand-x").trust_status == "TRUSTED"


def test_spec_round_trip(session: Session) -> None:
    specs = load_registry(REPO_ROOT / "sources.yaml")
    sync(session, specs)
    session.commit()
    row = session.get(Source, specs[0].id)
    restored = spec_from_row(row)
    assert isinstance(restored, SourceSpec)
    assert restored.allowed_hosts == specs[0].allowed_hosts
    assert restored.base_url == specs[0].base_url


# ---------------------------------------------------------------- LLM


def test_the_system_runs_with_no_llm_configured() -> None:
    provider = build_provider(Configuration(), Secrets(_env_file=None))
    assert isinstance(provider, NullProvider)
    result = extract_semantic(provider, "edital_extraction_v1", "texto")
    assert result.status == "DISABLED"
    assert result.usable is False


def test_prompts_are_versioned_on_disk() -> None:
    text = load_prompt("edital_extraction_v1", REPO_ROOT / "prompts")
    assert "NOT_FOUND" in text
    assert "nunca é local de lotação" in text.lower() or "nunca é local de lota" in text.lower()
    load_prompt("edital_validation_v1", REPO_ROOT / "prompts")


def test_provider_outage_degrades_instead_of_failing() -> None:
    class Broken:
        name, model, model_version = "broken", "m", "v"

        def complete(self, prompt: str, document: str) -> str:
            raise httpx.ConnectError("sem rede")

    result = extract_semantic(Broken(), "edital_extraction_v1", "texto")
    assert result.status == "UNAVAILABLE"
    assert result.usable is False


def test_malformed_model_output_is_discarded() -> None:
    for raw in ("nao e json", "[]", '{"fields": "texto"}', '{"fields": {"salary": {}}}'):
        fields, positions, problem = parse_payload(raw)
        assert fields == {}


def test_a_claim_without_quoted_evidence_is_rejected() -> None:
    payload = json.dumps(
        {
            "fields": {
                "exam_date": {
                    "value": "2026-11-22",
                    "status": "FOUND",
                    "confidence": 0.99,
                    "raw_evidence": "",
                }
            }
        }
    )
    fields, _, _ = parse_payload(payload)
    assert fields == {}


def test_the_model_may_not_touch_eligibility_fields() -> None:
    payload = json.dumps(
        {
            "fields": {
                "exam_date": {
                    "value": "2026-11-22",
                    "status": "FOUND",
                    "confidence": 0.9,
                    "raw_evidence": "prova em 22/11/2026",
                },
                "weekly_workload": {
                    "value": 20,
                    "status": "FOUND",
                    "confidence": 1.0,
                    "raw_evidence": "20h",
                },
                "assignment_location": {
                    "value": "Rio Branco",
                    "status": "FOUND",
                    "confidence": 1.0,
                    "raw_evidence": "lotação",
                },
            }
        }
    )
    fields, _, _ = parse_payload(payload)
    assert set(fields) == {"exam_date"}
    assert fields["exam_date"]["confidence"] <= 0.9  # never outranks a table parse


def test_not_found_is_never_turned_into_a_value() -> None:
    payload = json.dumps(
        {
            "fields": {
                "exam_date": {
                    "value": None,
                    "status": "NOT_FOUND",
                    "confidence": 0.0,
                    "raw_evidence": "nada",
                }
            }
        }
    )
    assert parse_payload(payload)[0] == {}


def test_fill_gaps_only_fills_empty_fields() -> None:
    from datetime import date

    from sentinela.domain import OpportunityDraft

    draft = OpportunityDraft(institution="X", name="Y", exam_date=date(2026, 11, 22))
    result = LLMResult(
        "OK",
        fields={
            "exam_date": {
                "value": "2027-01-01",
                "status": "FOUND",
                "confidence": 0.9,
                "raw_evidence": "x",
            },
            "validity": {
                "value": "2 anos",
                "status": "FOUND",
                "confidence": 0.8,
                "raw_evidence": "validade de 2 anos",
            },
        },
        prompt_version="edital_extraction_v1",
    )
    applied = fill_gaps(draft, result, "https://x.gov.br")
    assert draft.exam_date == date(2026, 11, 22)  # untouched
    assert draft.validity == "2 anos"
    assert applied == ["validity"]
    assert draft.evidence["validity"].extraction_method.startswith("llm:")


def test_cache_key_covers_every_dimension() -> None:
    base = cache_key("hash", "p1", "m", "v1")
    assert base != cache_key("hash2", "p1", "m", "v1")
    assert base != cache_key("hash", "p2", "m", "v1")
    assert base != cache_key("hash", "p1", "m2", "v1")
    assert base != cache_key("hash", "p1", "m", "v2")
    assert base == cache_key("hash", "p1", "m", "v1")


# ---------------------------------------------------------------- backup


def test_backup_round_trips_through_a_verified_file(
    engine: Engine, session: Session, source: Source, tmp_path: Path
) -> None:
    session.add(
        Opportunity(
            dedup_key="k",
            institution="Prefeitura",
            name="Concurso",
            first_seen_at=now(),
            last_seen_at=now(),
        )
    )
    session.commit()

    result = backup_module.dump(engine, tmp_path)
    assert result.path.is_file()
    assert result.rows > 0
    ok, detail = backup_module.verify(result.path)
    assert ok is True, detail

    if result.method == "json":
        target = backup_module.create_engine_for_restore = None  # noqa: F841
        from sentinela.db import create_engine as make
        from sentinela.models import Base

        other = make("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(other)
        inserted = backup_module.restore_json(other, result.path)
        assert inserted.get("opportunities") == 1
        with other.connect() as connection:
            restored = connection.execute(
                sa.select(Opportunity.name).select_from(Opportunity.__table__)
            ).scalar_one()
        assert restored == "Concurso"


def test_a_corrupt_backup_fails_verification(tmp_path: Path) -> None:
    bad = tmp_path / "sentinela-quebrado.json.gz"
    bad.write_bytes(b"isto nao e gzip" * 10)
    ok, detail = backup_module.verify(bad)
    assert ok is False
    assert detail


def test_missing_backup_fails_verification(tmp_path: Path) -> None:
    assert backup_module.verify(tmp_path / "inexistente.gz")[0] is False


def test_retention_removes_only_old_files(tmp_path: Path) -> None:
    import os
    import time

    old = tmp_path / "sentinela-20200101T000000Z.json.gz"
    new = tmp_path / "sentinela-20260101T000000Z.json.gz"
    for path in (old, new):
        path.write_bytes(b"x")
    os.utime(old, (time.time() - 86400 * 60, time.time() - 86400 * 60))
    removed = backup_module.prune(tmp_path, keep_days=30)
    assert removed == [old]
    assert new.exists()


def test_restore_refuses_to_overwrite_a_populated_database(
    engine: Engine, session: Session, source: Source, tmp_path: Path
) -> None:
    session.add(
        Opportunity(
            dedup_key="k", institution="P", name="C", first_seen_at=now(), last_seen_at=now()
        )
    )
    session.commit()
    result = backup_module.dump(engine, tmp_path)
    if result.method != "json":
        pytest.skip("pg_dump disponível: restauração é feita com psql")
    with pytest.raises(RuntimeError, match="nao esta vazio"):
        backup_module.restore_json(engine, result.path)


# ---------------------------------------------------------------- issues & report


def test_issue_body_carries_diagnostics_and_no_secrets(
    session: Session, source: Source, config: Configuration
) -> None:
    state = session.get(SourceHealth, source.id)
    state.state, state.consecutive_failures, state.last_status_code = "OPEN", 7, 503
    state.last_error = "ConnectTimeout"
    session.commit()
    body = issue_body(source, state, config)
    assert ISSUE_MARKER in body
    assert f"source_id: {source.id}" in body
    assert "503" in body and "ConnectTimeout" in body
    for forbidden in ("DATABASE_URL", "postgresql://", "TELEGRAM", "Bearer "):
        assert forbidden not in body


def test_no_duplicate_issue_for_the_same_source(
    session: Session, source: Source, config: Configuration
) -> None:
    state = session.get(SourceHealth, source.id)
    state.state, state.consecutive_failures = "OPEN", 9
    session.commit()
    secrets = Secrets(_env_file=None, github_repository="u/r")
    object.__setattr__(secrets, "github_token", secrets.github_token)
    secrets.github_token = type(secrets.github_token)("tok")

    posted: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, json=[{"number": 42, "body": f"{ISSUE_MARKER}\nsource_id: {source.id}"}]
            )
        posted.append(str(request.url))
        return httpx.Response(201, json={"number": 99})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        created = report_degraded_sources(session, config, secrets, client)
    assert created == []
    assert posted == []
    assert session.get(SourceHealth, source.id).issue_number == 42


def test_report_has_every_required_section(
    session: Session, source: Source, config: Configuration
) -> None:
    content = report_module.build(session, config)
    for section in report_module.SECTIONS:
        assert f"## {section}" in content
    assert "America/Rio_Branco" in content


def test_report_is_written_to_disk(
    session: Session, source: Source, config: Configuration, tmp_path: Path
) -> None:
    path = report_module.write(session, config, tmp_path)
    assert path.name == "latest.md"
    assert path.read_text(encoding="utf-8").startswith("# Sentinela AC")
