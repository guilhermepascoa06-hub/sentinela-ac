"""Persistent schema. PostgreSQL is the source of truth; SQLite is used only by tests.

Design rules encoded here:
  * nothing important is overwritten -- every mutation of an opportunity creates a version row;
  * every alert is deduplicated by a database unique key, not by application memory;
  * raw evidence is stored next to the interpreted value so any claim can be re-audited.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSON everywhere, JSONB when the dialect supports it.
JSONType = sa.JSON().with_variant(JSONB(), "postgresql")
UUIDType = sa.Uuid(as_uuid=False)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JSONType,
        list[str]: JSONType,
        list[dict[str, Any]]: JSONType,
        Decimal: sa.Numeric(12, 2),
        datetime: sa.DateTime(timezone=True),
        date: sa.Date(),
        str: sa.Text(),
    }


def _pk() -> Mapped[str]:
    return mapped_column(UUIDType, primary_key=True, default=new_id)


class Source(Base):
    """Mirror of sources.yaml. The YAML is the editable registry; this is runtime state."""

    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    name: Mapped[str]
    institution: Mapped[str]
    base_url: Mapped[str]
    official: Mapped[bool] = mapped_column(default=True)
    trust_level: Mapped[int] = mapped_column(default=1)
    priority: Mapped[int] = mapped_column(default=10)
    adapter: Mapped[str] = mapped_column(sa.String(32), default="generic")
    discovery_method: Mapped[str] = mapped_column(sa.String(32), default="html")
    enabled: Mapped[bool] = mapped_column(default=True)
    # CANDIDATE sources are discovered automatically and never feed primary alerts.
    trust_status: Mapped[str] = mapped_column(sa.String(16), default="CANDIDATE")
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    limitations: Mapped[str] = mapped_column(default="")
    discovered_from: Mapped[str | None]
    validated_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())

    health: Mapped[SourceHealth | None] = relationship(back_populates="source", uselist=False)


class SourceHealth(Base):
    __tablename__ = "source_health"
    source_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(sa.String(16), default="HEALTHY")
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    total_attempts: Mapped[int] = mapped_column(default=0)
    total_successes: Mapped[int] = mapped_column(default=0)
    parser_attempts: Mapped[int] = mapped_column(default=0)
    parser_successes: Mapped[int] = mapped_column(default=0)
    last_attempt: Mapped[datetime | None]
    last_success: Mapped[datetime | None]
    last_status_code: Mapped[int | None]
    last_response_ms: Mapped[int | None]
    last_error: Mapped[str | None]
    # Rolling evidence used by schema-drift detection.
    content_fingerprint: Mapped[str | None] = mapped_column(sa.String(64))
    documents_last_run: Mapped[int | None]
    documents_median: Mapped[float | None]
    document_history: Mapped[list[str]] = mapped_column(default=list)
    open_until: Mapped[datetime | None]
    etag: Mapped[str | None]
    last_modified: Mapped[str | None]
    issue_number: Mapped[int | None]
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now()
    )

    source: Mapped[Source] = relationship(back_populates="health")


class MonitorRun(Base):
    """Heartbeat. The watchdog reads this table and nothing else."""

    __tablename__ = "monitor_runs"
    id: Mapped[str] = _pk()
    kind: Mapped[str] = mapped_column(sa.String(16), default="daily")
    trigger: Mapped[str] = mapped_column(sa.String(32), default="manual")
    status: Mapped[str] = mapped_column(sa.String(16), default="RUNNING", index=True)
    started_at: Mapped[datetime] = mapped_column(index=True)
    finished_at: Mapped[datetime | None]
    sources_attempted: Mapped[int] = mapped_column(default=0)
    sources_successful: Mapped[int] = mapped_column(default=0)
    sources_failed: Mapped[int] = mapped_column(default=0)
    sources_skipped: Mapped[int] = mapped_column(default=0)
    documents_discovered: Mapped[int] = mapped_column(default=0)
    documents_changed: Mapped[int] = mapped_column(default=0)
    opportunities_created: Mapped[int] = mapped_column(default=0)
    opportunities_updated: Mapped[int] = mapped_column(default=0)
    notifications_sent: Mapped[int] = mapped_column(default=0)
    errors: Mapped[int] = mapped_column(default=0)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)


class SourceRun(Base):
    __tablename__ = "source_runs"
    id: Mapped[str] = _pk()
    run_id: Mapped[str] = mapped_column(
        UUIDType, sa.ForeignKey("monitor_runs.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[str] = mapped_column(sa.String(64), sa.ForeignKey("sources.id"), index=True)
    status: Mapped[str] = mapped_column(sa.String(16))
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    http_status: Mapped[int | None]
    response_ms: Mapped[int | None]
    documents_discovered: Mapped[int] = mapped_column(default=0)
    documents_new: Mapped[int] = mapped_column(default=0)
    documents_changed: Mapped[int] = mapped_column(default=0)
    drift_detected: Mapped[bool] = mapped_column(default=False)
    recovery_method: Mapped[str | None] = mapped_column(sa.String(32))
    error: Mapped[str | None]
    __table_args__ = (sa.UniqueConstraint("run_id", "source_id", name="uq_source_run"),)


class Document(Base):
    """A URL we have seen. Identity is the normalized URL; content lives in versions."""

    __tablename__ = "documents"
    id: Mapped[str] = _pk()
    source_id: Mapped[str] = mapped_column(sa.String(64), sa.ForeignKey("sources.id"), index=True)
    url: Mapped[str] = mapped_column(sa.String(1024), unique=True)
    canonical_url: Mapped[str] = mapped_column(sa.String(1024), index=True)
    title: Mapped[str | None]
    media_type: Mapped[str] = mapped_column(sa.String(32), default="html")
    document_type: Mapped[str] = mapped_column(sa.String(32), default="UNKNOWN")
    publication_date: Mapped[date | None]
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    current_version_id: Mapped[str | None] = mapped_column(UUIDType)
    # REVIEW keeps a failed extraction alive instead of silently dropping it.
    processing_state: Mapped[str] = mapped_column(sa.String(16), default="PENDING", index=True)
    review_reason: Mapped[str | None]
    attempts: Mapped[int] = mapped_column(default=0)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    id: Mapped[str] = _pk()
    document_id: Mapped[str] = mapped_column(
        UUIDType, sa.ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    content_hash: Mapped[str] = mapped_column(sa.String(64), index=True)
    metadata_hash: Mapped[str] = mapped_column(sa.String(64), default="")
    byte_size: Mapped[int] = mapped_column(default=0)
    page_count: Mapped[int | None]
    text_status: Mapped[str] = mapped_column(sa.String(24), default="UNKNOWN")
    extraction_method: Mapped[str] = mapped_column(sa.String(32), default="")
    parser_version: Mapped[str] = mapped_column(sa.String(32), default="")
    semantic_version: Mapped[str] = mapped_column(sa.String(64), default="")
    http_status: Mapped[int | None]
    etag: Mapped[str | None]
    last_modified: Mapped[str | None]
    downloaded_at: Mapped[datetime]
    detected_at: Mapped[datetime]
    __table_args__ = (
        sa.UniqueConstraint("document_id", "content_hash", name="uq_document_version"),
    )


class RawSnapshot(Base):
    """Extracted text kept for audit. Downloaded bytes are data, never executed."""

    __tablename__ = "raw_snapshots"
    id: Mapped[str] = _pk()
    document_version_id: Mapped[str] = mapped_column(
        UUIDType, sa.ForeignKey("document_versions.id", ondelete="CASCADE"), unique=True
    )
    text: Mapped[str] = mapped_column(default="")
    truncated: Mapped[bool] = mapped_column(default=False)
    captured_at: Mapped[datetime]


class Opportunity(Base):
    __tablename__ = "opportunities"
    id: Mapped[str] = _pk()
    dedup_key: Mapped[str] = mapped_column(sa.String(128), unique=True, index=True)
    institution: Mapped[str]
    name: Mapped[str]
    edital_number: Mapped[str | None] = mapped_column(sa.String(64))
    employment_type: Mapped[str] = mapped_column(sa.String(24), default="UNKNOWN")
    employment_regime: Mapped[str | None] = mapped_column(sa.String(64))
    organizing_board: Mapped[str | None]
    status: Mapped[str] = mapped_column(sa.String(24), default="EDITAL_PUBLISHED", index=True)
    publication_date: Mapped[date | None]
    registration_start: Mapped[date | None]
    registration_deadline: Mapped[date | None]
    exam_date: Mapped[date | None]
    exam_location: Mapped[str | None]
    application_fee: Mapped[Decimal | None]
    fee_exemption: Mapped[str | None]
    fee_exemption_deadline: Mapped[date | None]
    payment_deadline: Mapped[date | None]
    selection_stages: Mapped[list[str]] = mapped_column(default=list)
    validity: Mapped[str | None]
    official_edital_url: Mapped[str | None] = mapped_column(sa.String(1024))
    official_institution_url: Mapped[str | None] = mapped_column(sa.String(1024))
    official_application_url: Mapped[str | None] = mapped_column(sa.String(1024))
    organizing_board_url: Mapped[str | None] = mapped_column(sa.String(1024))
    best_trust_level: Mapped[int] = mapped_column(default=4)
    # trust_level * 10, minus a bonus when the document carried a real cargo table. A
    # listing page must never overwrite a field that an opening edital established.
    best_authority: Mapped[int] = mapped_column(default=99)
    score: Mapped[int] = mapped_column(default=0, index=True)
    rank: Mapped[str] = mapped_column(sa.String(16), default="LOW PRIORITY")
    confidence: Mapped[str] = mapped_column(sa.String(8), default="LOW")
    workload_classification: Mapped[str] = mapped_column(sa.String(16), default="UNKNOWN")
    eligible: Mapped[bool] = mapped_column(default=False, index=True)
    needs_review: Mapped[bool] = mapped_column(default=True, index=True)
    reasons: Mapped[list[str]] = mapped_column(default=list)
    conflicts: Mapped[list[dict[str, Any]]] = mapped_column(default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Every source that independently confirmed this opportunity.
    evidence_sources: Mapped[list[str]] = mapped_column(default=list)
    content_hash: Mapped[str] = mapped_column(sa.String(64), default="")
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    version: Mapped[int] = mapped_column(default=1)

    positions: Mapped[list[Position]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan", lazy="selectin"
    )


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[str] = _pk()
    opportunity_id: Mapped[str] = mapped_column(
        UUIDType, sa.ForeignKey("opportunities.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(sa.String(160), index=True)
    name: Mapped[str]
    education: Mapped[str | None] = mapped_column(sa.String(32))
    qualification_category: Mapped[str] = mapped_column(sa.String(48), default="UNKNOWN")
    additional_qualifications: Mapped[list[str]] = mapped_column(default=list)
    requirements: Mapped[str | None]
    vacancies: Mapped[int | None]
    reserve_list: Mapped[bool | None]
    reserve_count: Mapped[int | None]
    assignment_location: Mapped[str | None]
    possible_assignment_locations: Mapped[list[str]] = mapped_column(default=list)
    assignment_confirmed: Mapped[bool] = mapped_column(default=False)
    salary: Mapped[Decimal | None]
    benefits: Mapped[str | None]
    weekly_workload: Mapped[float | None]
    daily_workload: Mapped[float | None]
    workload_classification: Mapped[str] = mapped_column(sa.String(16), default="UNKNOWN")
    # A placeholder created from a document with no cargo table. Dropped as soon as the
    # real edital supplies actual cargos.
    synthetic: Mapped[bool] = mapped_column(default=False)
    score: Mapped[int] = mapped_column(default=0)
    rank: Mapped[str] = mapped_column(sa.String(16), default="LOW PRIORITY")
    confidence: Mapped[str] = mapped_column(sa.String(8), default="LOW")
    eligible: Mapped[bool] = mapped_column(default=False, index=True)
    primary_alert: Mapped[bool] = mapped_column(default=False)
    needs_review: Mapped[bool] = mapped_column(default=True)
    reasons: Mapped[list[str]] = mapped_column(default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    __table_args__ = (sa.UniqueConstraint("opportunity_id", "slug", name="uq_position_slug"),)

    opportunity: Mapped[Opportunity] = relationship(back_populates="positions")


class OpportunityVersion(Base):
    """Full immutable snapshot plus the field-level diff that produced it."""

    __tablename__ = "opportunity_versions"
    id: Mapped[str] = _pk()
    opportunity_id: Mapped[str] = mapped_column(
        UUIDType, sa.ForeignKey("opportunities.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int]
    snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)
    changes: Mapped[list[dict[str, Any]]] = mapped_column(default=list)
    source_id: Mapped[str | None] = mapped_column(sa.String(64))
    source_url: Mapped[str | None] = mapped_column(sa.String(1024))
    document_id: Mapped[str | None] = mapped_column(UUIDType)
    document_version_id: Mapped[str | None] = mapped_column(UUIDType)
    detected_at: Mapped[datetime]
    __table_args__ = (
        sa.UniqueConstraint("opportunity_id", "version", name="uq_opportunity_version"),
    )


class Deadline(Base):
    __tablename__ = "deadlines"
    id: Mapped[str] = _pk()
    opportunity_id: Mapped[str] = mapped_column(
        UUIDType, sa.ForeignKey("opportunities.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(sa.String(24))
    due_date: Mapped[date] = mapped_column(index=True)
    description: Mapped[str] = mapped_column(default="")
    source_url: Mapped[str | None] = mapped_column(sa.String(1024))
    active: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime]
    __table_args__ = (sa.UniqueConstraint("opportunity_id", "kind", name="uq_deadline_kind"),)


class Notification(Base):
    """Outbox. `idempotency_key` is the only thing preventing duplicate alerts."""

    __tablename__ = "notifications"
    id: Mapped[str] = _pk()
    idempotency_key: Mapped[str] = mapped_column(sa.String(160), unique=True, index=True)
    channel: Mapped[str] = mapped_column(sa.String(16))
    category: Mapped[str] = mapped_column(sa.String(32), index=True)
    opportunity_id: Mapped[str | None] = mapped_column(UUIDType)
    subject: Mapped[str] = mapped_column(default="")
    body: Mapped[str] = mapped_column(default="")
    status: Mapped[str] = mapped_column(sa.String(16), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    external_id: Mapped[str | None] = mapped_column(sa.String(128))
    last_error: Mapped[str | None]
    not_before: Mapped[datetime | None]
    created_at: Mapped[datetime]
    sent_at: Mapped[datetime | None]
    run_id: Mapped[str | None] = mapped_column(UUIDType)


class ClassificationHistory(Base):
    __tablename__ = "classification_history"
    id: Mapped[str] = _pk()
    opportunity_id: Mapped[str | None] = mapped_column(UUIDType, index=True)
    position_id: Mapped[str | None] = mapped_column(UUIDType)
    document_id: Mapped[str | None] = mapped_column(UUIDType)
    classifier: Mapped[str] = mapped_column(sa.String(48))
    classifier_version: Mapped[str] = mapped_column(sa.String(32))
    outcome: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime]


class ExtractionResult(Base):
    """Cache for expensive semantic extraction, keyed exactly as the spec requires."""

    __tablename__ = "extraction_results"
    id: Mapped[str] = _pk()
    document_hash: Mapped[str] = mapped_column(sa.String(64), index=True)
    prompt_version: Mapped[str] = mapped_column(sa.String(64))
    model: Mapped[str] = mapped_column(sa.String(64))
    model_version: Mapped[str] = mapped_column(sa.String(64))
    status: Mapped[str] = mapped_column(sa.String(16), default="OK")
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime]
    __table_args__ = (
        sa.UniqueConstraint(
            "document_hash", "prompt_version", "model", "model_version", name="uq_extraction_cache"
        ),
    )


class ErrorLog(Base):
    __tablename__ = "errors"
    id: Mapped[str] = _pk()
    run_id: Mapped[str | None] = mapped_column(UUIDType, index=True)
    source_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    document_id: Mapped[str | None] = mapped_column(UUIDType)
    stage: Mapped[str] = mapped_column(sa.String(32))
    kind: Mapped[str] = mapped_column(sa.String(48))
    message: Mapped[str] = mapped_column(default="")
    context: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(index=True)


class SystemEvent(Base):
    __tablename__ = "system_events"
    id: Mapped[str] = _pk()
    run_id: Mapped[str | None] = mapped_column(UUIDType, index=True)
    kind: Mapped[str] = mapped_column(sa.String(48), index=True)
    severity: Mapped[str] = mapped_column(sa.String(16), default="INFO")
    message: Mapped[str] = mapped_column(default="")
    context: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(index=True)


class Tracking(Base):
    """O que o usuário decidiu sobre um cargo. Nunca um fato lido de edital.

    Fica em tabela própria de propósito: "eu me inscrevi" não é "as inscrições estão
    abertas". Misturar as duas coisas daria ao palpite do usuário o peso do documento
    oficial, que é exatamente o que este sistema existe para não fazer.
    """

    __tablename__ = "tracking"
    id: Mapped[str] = _pk()
    position_id: Mapped[str] = mapped_column(
        UUIDType, sa.ForeignKey("positions.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(sa.String(16), default="INTERESTED")
    note: Mapped[str | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
