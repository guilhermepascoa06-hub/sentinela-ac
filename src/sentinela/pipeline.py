"""The monitoring run.

Stages are idempotent and independently retryable: rerunning a day changes nothing unless
the sources changed. Every stage records what it did, and a stage that fails records the
failure and lets the run continue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from sentinela import alerts, diff, health
from sentinela.collector import FoundDocument, SourceOutcome, safe_collect
from sentinela.config import Configuration, Secrets
from sentinela.dedup import Candidate, dedup_key, find_match, position_slug
from sentinela.domain import OpportunityDraft, PositionDraft, digest, now, utc
from sentinela.eligibility import effective_status, evaluate
from sentinela.fetch import Fetcher
from sentinela.llm import build_provider, cache_key, extract_semantic, fill_gaps
from sentinela.logging import RunLogger, get
from sentinela.models import (
    ClassificationHistory,
    Deadline,
    Document,
    DocumentVersion,
    ErrorLog,
    ExtractionResult,
    MonitorRun,
    Notification,
    Opportunity,
    OpportunityVersion,
    Position,
    RawSnapshot,
    Source,
    SourceHealth,
    SystemEvent,
)
from sentinela.registry import spec_from_row
from sentinela.structure import build_opportunity

MAX_SNAPSHOT_CHARS = 200_000
LOCATE_PROMPT = "edital_locate_v1"
DEADLINE_FIELDS = (
    ("registration", "registration_deadline", "Encerramento das inscrições"),
    ("exam", "exam_date", "Data da prova"),
    ("fee_exemption", "fee_exemption_deadline", "Prazo de isenção da taxa"),
    ("payment", "payment_deadline", "Prazo de pagamento da taxa"),
)


@dataclass
class RunReport:
    run_id: str
    status: str = "RUNNING"
    started_at: datetime = field(default_factory=now)
    finished_at: datetime | None = None
    sources_attempted: int = 0
    sources_successful: int = 0
    sources_failed: int = 0
    sources_skipped: int = 0
    documents_discovered: int = 0
    documents_new: int = 0
    documents_changed: int = 0
    opportunities_created: int = 0
    opportunities_updated: int = 0
    notifications_queued: int = 0
    notifications_sent: int = 0
    errors: int = 0
    drifted: list[str] = field(default_factory=list)
    rendered: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    review_queue: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


def record_error(
    session: Session,
    run_id: str | None,
    *,
    stage: str,
    kind: str,
    message: str,
    source_id: str | None = None,
    document_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    session.add(
        ErrorLog(
            run_id=run_id,
            source_id=source_id,
            document_id=document_id,
            stage=stage,
            kind=kind,
            message=message[:2000],
            context=context or {},
            created_at=now(),
        )
    )


def record_event(
    session: Session,
    run_id: str | None,
    *,
    kind: str,
    message: str,
    severity: str = "INFO",
    context: dict[str, Any] | None = None,
) -> None:
    session.add(
        SystemEvent(
            run_id=run_id,
            kind=kind,
            severity=severity,
            message=message[:2000],
            context=context or {},
            created_at=now(),
        )
    )


# ---------------------------------------------------------------- persistence helpers


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_jsonable(item) for item in value]
    return value


def position_snapshot(position: Position) -> dict[str, Any]:
    return _to_jsonable(
        {
            "slug": position.slug,
            "name": position.name,
            "education": position.education,
            "qualification_category": position.qualification_category,
            "additional_qualifications": list(position.additional_qualifications or []),
            "requirements": position.requirements,
            "vacancies": position.vacancies,
            "reserve_count": position.reserve_count,
            "salary": position.salary,
            "weekly_workload": position.weekly_workload,
            "daily_workload": position.daily_workload,
            "assignment_location": position.assignment_location,
            "assignment_confirmed": position.assignment_confirmed,
            "benefits": position.benefits,
            "workload_classification": position.workload_classification,
            "score": position.score,
            "eligible": position.eligible,
        }
    )


def opportunity_snapshot(opportunity: Opportunity) -> dict[str, Any]:
    return _to_jsonable(
        {
            "institution": opportunity.institution,
            "name": opportunity.name,
            "edital_number": opportunity.edital_number,
            "status": opportunity.status,
            "employment_type": opportunity.employment_type,
            "employment_regime": opportunity.employment_regime,
            "organizing_board": opportunity.organizing_board,
            "publication_date": opportunity.publication_date,
            "registration_start": opportunity.registration_start,
            "registration_deadline": opportunity.registration_deadline,
            "exam_date": opportunity.exam_date,
            "exam_location": opportunity.exam_location,
            "application_fee": opportunity.application_fee,
            "fee_exemption_deadline": opportunity.fee_exemption_deadline,
            "payment_deadline": opportunity.payment_deadline,
            "selection_stages": list(opportunity.selection_stages or []),
            "validity": opportunity.validity,
            "official_edital_url": opportunity.official_edital_url,
            "official_institution_url": opportunity.official_institution_url,
            "official_application_url": opportunity.official_application_url,
            "score": opportunity.score,
            "rank": opportunity.rank,
            "confidence": opportunity.confidence,
            "eligible": opportunity.eligible,
            "positions": [position_snapshot(item) for item in opportunity.positions],
        }
    )


def queue_notification(
    session: Session,
    *,
    key: str,
    category: str,
    body: str,
    channels: list[str],
    run_id: str | None = None,
    opportunity_id: str | None = None,
    subject: str = "",
) -> int:
    """Insert one outbox row per channel. The unique key makes a repeat a silent no-op."""
    queued = 0
    for channel in channels:
        scoped = f"{channel}:{key}"
        exists = session.execute(
            sa.select(Notification.id).where(Notification.idempotency_key == scoped)
        ).first()
        if exists:
            continue
        session.add(
            Notification(
                idempotency_key=scoped,
                channel=channel,
                category=category,
                opportunity_id=opportunity_id,
                subject=subject[:200],
                body=body,
                status="PENDING",
                created_at=now(),
                run_id=run_id,
            )
        )
        queued += 1
    try:
        session.flush()
    except sa.exc.IntegrityError:
        # Another run inserted the same key between the check and the flush. That is the
        # unique index doing exactly its job; the alert is already queued.
        session.rollback()
        return 0
    return queued


# ---------------------------------------------------------------- document persistence


def persist_document(
    session: Session, source_id: str, found: FoundDocument, report: RunReport
) -> tuple[Document, DocumentVersion, bool]:
    """Upsert the document and append a version only when the content actually changed."""
    document = session.execute(
        sa.select(Document).where(Document.url == found.url)
    ).scalar_one_or_none()
    fresh = document is None
    if document is None:
        document = Document(
            source_id=source_id,
            url=found.url,
            canonical_url=found.url,
            title=found.title[:300] if found.title else None,
            media_type=found.media_type,
            first_seen_at=found.fetched_at,
            last_seen_at=found.fetched_at,
            processing_state="PENDING",
        )
        session.add(document)
        session.flush()
        report.documents_new += 1
    else:
        document.last_seen_at = found.fetched_at
        if found.title:
            document.title = found.title[:300]

    metadata_hash = digest([found.media_type, found.document.page_count, found.title])
    existing = session.execute(
        sa.select(DocumentVersion).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.content_hash == found.content_hash,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.downloaded_at = found.fetched_at
        document.current_version_id = existing.id
        return document, existing, False

    from sentinela.domain import PARSER_VERSION

    version = DocumentVersion(
        document_id=document.id,
        content_hash=found.content_hash,
        metadata_hash=metadata_hash,
        byte_size=len(found.content),
        page_count=found.document.page_count,
        text_status=found.document.status,
        extraction_method=found.document.method,
        parser_version=PARSER_VERSION,
        http_status=found.http_status,
        etag=found.etag,
        last_modified=found.last_modified,
        downloaded_at=found.fetched_at,
        detected_at=now(),
    )
    session.add(version)
    session.flush()
    document.current_version_id = version.id
    text = found.document.text[:MAX_SNAPSHOT_CHARS]
    session.add(
        RawSnapshot(
            document_version_id=version.id,
            text=text,
            truncated=found.document.truncated or len(found.document.text) > MAX_SNAPSHOT_CHARS,
            captured_at=now(),
        )
    )
    if not fresh:
        report.documents_changed += 1
    return document, version, True


# ---------------------------------------------------------------- opportunity persistence


def _candidates(session: Session) -> list[Candidate]:
    rows = session.execute(
        sa.select(
            Opportunity.id,
            Opportunity.dedup_key,
            Opportunity.institution,
            Opportunity.name,
            Opportunity.edital_number,
            Opportunity.publication_date,
            Opportunity.organizing_board,
            Opportunity.official_edital_url,
            Opportunity.content_hash,
        )
    ).all()
    return [
        Candidate(
            id=row[0],
            dedup_key=row[1],
            institution=row[2],
            name=row[3],
            edital_number=row[4],
            publication_date=row[5],
            organizing_board=row[6],
            urls=(row[7],) if row[7] else (),
            document_hashes=(row[8],) if row[8] else (),
        )
        for row in rows
    ]


def _apply_position(target: Position, draft: PositionDraft, verdict: Any) -> None:
    target.name = draft.name
    for name in (
        "education",
        "qualification_category",
        "requirements",
        "vacancies",
        "reserve_list",
        "reserve_count",
        "assignment_location",
        "salary",
        "weekly_workload",
        "daily_workload",
        "benefits",
    ):
        incoming = getattr(draft, name)
        # A later document never erases a value an earlier, better document established.
        if incoming is not None:
            setattr(target, name, incoming)
    target.additional_qualifications = list(draft.additional_qualifications)
    if draft.possible_assignment_locations:
        target.possible_assignment_locations = list(draft.possible_assignment_locations)
    target.assignment_confirmed = target.assignment_confirmed or draft.assignment_confirmed
    target.workload_classification = verdict.workload_classification
    target.score, target.rank, target.confidence = verdict.score, verdict.rank, verdict.confidence
    target.eligible, target.primary_alert = verdict.eligible, verdict.primary_alert
    target.needs_review, target.reasons = verdict.needs_review, list(verdict.reasons)
    target.evidence = {
        name: _to_jsonable(item.model_dump()) for name, item in draft.evidence.items()
    }


def _sync_deadlines(session: Session, opportunity: Opportunity) -> None:
    for kind, attribute, description in DEADLINE_FIELDS:
        value = getattr(opportunity, attribute)
        row = session.execute(
            sa.select(Deadline).where(
                Deadline.opportunity_id == opportunity.id, Deadline.kind == kind
            )
        ).scalar_one_or_none()
        if value is None:
            if row is not None:
                row.active = False
                row.updated_at = now()
            continue
        if row is None:
            session.add(
                Deadline(
                    opportunity_id=opportunity.id,
                    kind=kind,
                    due_date=value,
                    description=description,
                    source_url=opportunity.official_edital_url,
                    active=True,
                    updated_at=now(),
                )
            )
        else:
            row.due_date, row.active, row.updated_at = value, True, now()
    session.flush()


def upsert_opportunity(
    session: Session,
    draft: OpportunityDraft,
    *,
    source: Source,
    document: Document,
    version: DocumentVersion,
    config: Configuration,
    today: date,
    report: RunReport,
    run_id: str | None,
) -> tuple[Opportunity, list[dict[str, Any]], bool]:
    """Create or update, always leaving behind a version row and a field-level diff."""
    match, reason = find_match(
        draft,
        _candidates(session),
        url=draft.official_edital_url or "",
        document_hash=version.content_hash,
    )
    # How much this document may overwrite: source trust, improved when it actually
    # carried a cargo table. Without this a listing page from the same source replaced
    # fields that the opening edital had established.
    has_real_positions = any(not item.synthetic for item in draft.positions)
    authority = source.trust_level * 10 + (0 if has_real_positions else 5)

    created = match is None
    if match is None:
        opportunity = Opportunity(
            dedup_key=dedup_key(draft),
            institution=draft.institution,
            name=draft.name,
            first_seen_at=now(),
            last_seen_at=now(),
            version=0,
            best_trust_level=source.trust_level,
            best_authority=authority,
        )
        session.add(opportunity)
        session.flush()
        previous: dict[str, Any] = {}
        previous_positions: list[dict[str, Any]] = []
    else:
        existing = session.get(Opportunity, match.id)
        if existing is None:  # deleted between the lookup and here
            raise RuntimeError(f"Oportunidade {match.id} desapareceu durante a execução")
        opportunity = existing
        previous = opportunity_snapshot(opportunity)
        previous_positions = list(previous.get("positions") or [])

    better = authority <= (opportunity.best_authority or 99)
    for name in (
        "edital_number",
        "publication_date",
        "employment_regime",
        "organizing_board",
        "registration_start",
        "registration_deadline",
        "exam_date",
        "exam_location",
        "application_fee",
        "fee_exemption",
        "fee_exemption_deadline",
        "payment_deadline",
        "validity",
        "official_edital_url",
        "official_institution_url",
        "official_application_url",
        "organizing_board_url",
    ):
        incoming = getattr(draft, name)
        if incoming is None:
            continue
        current = getattr(opportunity, name)
        if current is None or better:
            setattr(opportunity, name, incoming)
    if draft.employment_type != "UNKNOWN" and (opportunity.employment_type == "UNKNOWN" or better):
        opportunity.employment_type = draft.employment_type
    if draft.selection_stages:
        opportunity.selection_stages = list(draft.selection_stages)
    if better or not opportunity.name:
        opportunity.name = draft.name
    opportunity.institution = opportunity.institution or draft.institution
    opportunity.best_trust_level = min(opportunity.best_trust_level or 4, source.trust_level)
    opportunity.best_authority = min(opportunity.best_authority or 99, authority)
    opportunity.evidence = {
        name: _to_jsonable(item.model_dump()) for name, item in draft.evidence.items()
    }
    if draft.conflicts:
        opportunity.conflicts = list(opportunity.conflicts or []) + _to_jsonable(draft.conflicts)
    sources_seen = set(opportunity.evidence_sources or [])
    sources_seen.add(source.id)
    opportunity.evidence_sources = sorted(sources_seen)
    opportunity.content_hash = version.content_hash
    opportunity.last_seen_at = now()
    # Status is derived from the MERGED record, never from one document. Deriving it from
    # the draft let a dateless listing page push a certame back from REGISTRATION_OPEN to
    # EDITAL_PUBLISHED and emit a change alert that ran backwards in time.
    if draft.document_type == "CANCELLATION":
        opportunity.status = "CANCELLED"
    elif draft.document_type == "SUSPENSION":
        opportunity.status = "SUSPENDED"
    else:
        if draft.document_type in ("REOPENING", "EXTENSION"):
            opportunity.status = "EDITAL_PUBLISHED"  # a reopening clears a terminal state
        opportunity.status = effective_status(opportunity, today)

    existing_positions = {item.slug: item for item in opportunity.positions}
    any_eligible = any_review = False
    for position_draft in draft.positions:
        slug = position_slug(position_draft.name)
        # A placeholder from a page with no cargo table must not sit next to cargos read
        # from the real edital; it would show up as a phantom position in reports.
        if position_draft.synthetic and any(
            key != slug and not existing_positions[key].synthetic for key in existing_positions
        ):
            continue
        target = existing_positions.get(slug)
        if target is None:
            target = Position(
                opportunity_id=opportunity.id,
                slug=slug,
                name=position_draft.name,
                synthetic=position_draft.synthetic,
            )
            opportunity.positions.append(target)
            existing_positions[slug] = target
        merged = PositionDraft(
            name=position_draft.name,
            education=position_draft.education or target.education,
            additional_qualifications=position_draft.additional_qualifications,
            qualification_category=position_draft.qualification_category,
            vacancies=position_draft.vacancies
            if position_draft.vacancies is not None
            else target.vacancies,
            reserve_list=position_draft.reserve_list,
            reserve_count=position_draft.reserve_count,
            assignment_location=position_draft.assignment_location or target.assignment_location,
            possible_assignment_locations=position_draft.possible_assignment_locations,
            assignment_confirmed=position_draft.assignment_confirmed
            or bool(target.assignment_confirmed),
            salary=position_draft.salary if position_draft.salary is not None else target.salary,
            benefits=position_draft.benefits,
            weekly_workload=position_draft.weekly_workload
            if position_draft.weekly_workload is not None
            else target.weekly_workload,
            daily_workload=position_draft.daily_workload,
            requirements=position_draft.requirements or target.requirements,
            evidence=position_draft.evidence,
        )
        verdict = evaluate(draft, merged, config, today, source.trust_level)
        _apply_position(target, merged, verdict)
        session.add(
            ClassificationHistory(
                opportunity_id=opportunity.id,
                position_id=target.id,
                document_id=document.id,
                classifier="eligibility",
                classifier_version=_parser_version(),
                outcome=_to_jsonable(verdict.model_dump()),
                created_at=now(),
            )
        )
        any_eligible = any_eligible or verdict.eligible
        any_review = any_review or verdict.needs_review
    session.flush()

    if any(not item.synthetic for item in opportunity.positions):
        for stale in [item for item in opportunity.positions if item.synthetic]:
            opportunity.positions.remove(stale)
            session.delete(stale)
        session.flush()

    stored = list(opportunity.positions)
    best = max(stored, key=lambda item: item.score, default=None)
    opportunity.score = best.score if best else 0
    opportunity.rank = best.rank if best else "LOW PRIORITY"
    opportunity.confidence = best.confidence if best else "LOW"
    opportunity.workload_classification = best.workload_classification if best else "UNKNOWN"
    opportunity.eligible = any(item.eligible for item in stored) or any_eligible
    opportunity.needs_review = any(item.needs_review for item in stored) or any_review
    opportunity.reasons = list(draft.review_reasons)
    opportunity.version = (opportunity.version or 0) + 1
    session.flush()

    current = opportunity_snapshot(opportunity)
    changes = diff.compare(previous, current) if previous else []
    changes += diff.compare_positions(previous_positions, list(current.get("positions") or []))
    session.add(
        OpportunityVersion(
            opportunity_id=opportunity.id,
            version=opportunity.version,
            snapshot=current,
            changes=_to_jsonable(changes),
            source_id=source.id,
            source_url=document.url,
            document_id=document.id,
            document_version_id=version.id,
            detected_at=now(),
        )
    )
    _sync_deadlines(session, opportunity)
    if created:
        report.opportunities_created += 1
    elif changes:
        report.opportunities_updated += 1
    session.flush()
    return opportunity, changes, created


def _parser_version() -> str:
    from sentinela.domain import PARSER_VERSION

    return PARSER_VERSION


# ---------------------------------------------------------------- alert generation


def channels_for(config: Configuration) -> list[str]:
    mapping = (
        ("telegram", "telegram"),
        ("email", "email"),
        ("markdown", "markdown"),
        ("console", "console"),
    )
    return [name for key, name in mapping if config.notifications.get(key)]


def generate_alerts(
    session: Session,
    opportunity: Opportunity,
    changes: list[dict[str, Any]],
    created: bool,
    *,
    config: Configuration,
    report: RunReport,
    run_id: str | None,
    trust_level: int,
) -> None:
    """Only Level 1 or reliable Level 2 evidence produces a primary opportunity alert."""
    channels = channels_for(config)
    if not channels:
        return
    primary = [item for item in opportunity.positions if item.primary_alert]
    announced = 0
    if primary and trust_level <= 2:
        # Keyed on (opportunity, position), not on `created`: the listing page often
        # creates the record before the edital PDF makes it alert-worthy, and the user
        # must still receive "nova oportunidade" rather than "alteração" the first time.
        for position in primary:
            announced += queue_notification(
                session,
                key=alerts.idempotency_key("NEW_OPPORTUNITY", opportunity.id, position.slug),
                category="NEW_OPPORTUNITY",
                body=alerts.opportunity_message(opportunity, position),
                channels=channels,
                run_id=run_id,
                opportunity_id=opportunity.id,
                subject=f"{opportunity.institution} — {position.name}",
            )
    report.notifications_queued += announced
    if announced or created:
        return
    # A second document in the same run that merely enriches a just-announced opportunity
    # is not a change the user needs: the "nova oportunidade" message already has the data.
    if (
        run_id
        and session.execute(
            sa.select(Notification.id)
            .where(
                Notification.opportunity_id == opportunity.id,
                Notification.category == "NEW_OPPORTUNITY",
                Notification.run_id == run_id,
            )
            .limit(1)
        ).first()
    ):
        return

    interesting = diff.alertable(changes)
    if interesting and (primary or opportunity.eligible) and trust_level <= 2:
        rendered = diff.render(
            interesting,
            source=opportunity.institution,
            document=opportunity.official_edital_url or "",
        )
        report.notifications_queued += queue_notification(
            session,
            key=alerts.idempotency_key(
                "CHANGE",
                opportunity.id,
                opportunity.version,
                digest([item["field"] + str(item["new_value"]) for item in interesting]),
            ),
            category="CHANGE",
            body=alerts.change_message(opportunity, rendered),
            channels=channels,
            run_id=run_id,
            opportunity_id=opportunity.id,
            subject=f"Alteração — {opportunity.institution}",
        )
    # Workload becoming known turns a previously unrankable opportunity into an actionable one.
    for change in changes:
        if change["field"] == "weekly_workload" and change["old_value"] == "não informado":
            affected = next(
                (item for item in opportunity.positions if item.name == change.get("subject")), None
            )
            if affected is not None and affected.eligible:
                report.notifications_queued += queue_notification(
                    session,
                    key=alerts.idempotency_key("WORKLOAD_KNOWN", opportunity.id, affected.slug),
                    category="WORKLOAD_KNOWN",
                    body=alerts.opportunity_message(
                        opportunity, affected, category="WORKLOAD_KNOWN"
                    ),
                    channels=channels,
                    run_id=run_id,
                    opportunity_id=opportunity.id,
                    subject=f"Carga horária confirmada — {affected.name}",
                )


def generate_deadline_alerts(
    session: Session, config: Configuration, today: date, report: RunReport, run_id: str
) -> None:
    channels = channels_for(config)
    if not channels:
        return
    thresholds = {
        "registration": list(config.monitoring.registration_reminders) + [0],
        "exam": list(config.monitoring.exam_reminders) + [0],
        "fee_exemption": [3, 1, 0],
        "payment": [1, 0],
    }
    horizon = today + timedelta(days=max(max(values) for values in thresholds.values()))
    rows = session.execute(
        sa.select(Deadline, Opportunity)
        .join(Opportunity, Opportunity.id == Deadline.opportunity_id)
        .where(
            Deadline.active.is_(True),
            Deadline.due_date >= today,
            Deadline.due_date <= horizon,
            Opportunity.eligible.is_(True),
            Opportunity.status.notin_(["CANCELLED", "SUSPENDED", "EXPIRED"]),
        )
    ).all()
    for deadline, opportunity in rows:
        days = alerts.deadline_alerts(
            deadline.due_date, today, thresholds.get(deadline.kind, [1, 0])
        )
        if days is None:
            continue
        names = [item.name for item in opportunity.positions if item.eligible]
        report.notifications_queued += queue_notification(
            session,
            key=alerts.idempotency_key(
                "DEADLINE", opportunity.id, deadline.kind, deadline.due_date.isoformat(), days
            ),
            category="DEADLINE",
            body=alerts.deadline_message(
                opportunity,
                deadline.kind,
                deadline.due_date,
                (deadline.due_date - today).days,
                positions=names,
            ),
            channels=channels,
            run_id=run_id,
            opportunity_id=opportunity.id,
            subject=f"Prazo — {opportunity.institution}",
        )


def dispatch(
    session: Session,
    config: Configuration,
    secrets: Secrets,
    report: RunReport,
    logger: RunLogger,
    limit: int = 200,
) -> None:
    """Drain the outbox. Delivery status is written before the next row is attempted."""
    from sentinela.notifications import make_notifiers

    adapters = {adapter.name: adapter for adapter in make_notifiers(config, secrets)}
    if not adapters:
        return
    pending = (
        session.execute(
            sa.select(Notification)
            .where(
                # UNCONFIGURED rows are picked up again: the alert was never the
                # problem, the missing credential was, and it may exist by now.
                Notification.status.in_(["PENDING", "RETRY", "UNCONFIGURED"]),
                sa.or_(Notification.not_before.is_(None), Notification.not_before <= now()),
            )
            .order_by(Notification.created_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    max_attempts = int(config.notifications.get("max_attempts", 5))
    for row in pending:
        adapter = adapters.get(row.channel)
        if adapter is None:
            row.status, row.last_error = "SKIPPED", "Canal desabilitado na configuracao"
            continue
        row.attempts += 1
        result = adapter.send(row.idempotency_key, row.body)
        row.external_id = result.external_id or row.external_id
        row.last_error = result.error
        if result.status == "UNCONFIGURED":
            # Not an attempt against the message: do not burn the retry budget.
            row.attempts -= 1
            row.status = "UNCONFIGURED"
            session.flush()
            continue
        if result.status == "SENT":
            row.status, row.sent_at = "SENT", now()
            report.notifications_sent += 1
        elif result.status == "RETRY" and row.attempts < max_attempts:
            row.status = "RETRY"
            row.not_before = now() + timedelta(seconds=result.retry_after or 60 * row.attempts)
        elif result.status == "UNCERTAIN":
            # Telegram has no idempotency key: a blind retry can duplicate a real delivery.
            row.status = "UNCERTAIN"
            logger.warning(
                "Entrega incerta em %s: mantida para revisao",
                row.channel,
                extra={"stage": "notify"},
            )
        else:
            row.status = "BLOCKED" if result.status == "BLOCKED" else "FAILED"
        session.flush()


# ---------------------------------------------------------------- source stage


def process_source(
    session: Session,
    source: Source,
    outcome: SourceOutcome,
    *,
    config: Configuration,
    today: date,
    report: RunReport,
    run_id: str | None,
    logger: RunLogger,
    llm_provider: Any = None,
) -> None:
    spec = spec_from_row(source)
    for found in outcome.documents:
        report.documents_discovered += 1
        try:
            document, version, changed = persist_document(session, source.id, found, report)
            if (
                not changed
                and document.processing_state == "DONE"
                and not _semantic_pending(session, found, llm_provider, config)
            ):
                continue  # unchanged content: skip the expensive stages entirely
            document.attempts += 1
            if not found.document.usable:
                document.processing_state = "REVIEW"
                document.review_reason = (
                    f"Extracao {found.document.status} via {found.document.method}"
                )
                report.review_queue += 1
                record_error(
                    session,
                    run_id,
                    stage="extract",
                    kind=found.document.status,
                    message=document.review_reason,
                    source_id=source.id,
                    document_id=document.id,
                )
                continue
            draft = build_opportunity(
                found.document,
                found.content,
                found.url,
                institution=source.institution,
                city=config.profile.city,
                media_type=found.media_type,
                source_board=None,
                header_date=found.last_modified,
            )
            document.document_type = draft.document_type
            document.publication_date = draft.publication_date
            if not draft.material:
                # A listing or navigation page that names a concurso but states no edital
                # number, no dates and no cargo table. Storing it as an opportunity would
                # manufacture a certame that does not exist; it stays queued for retry.
                document.processing_state = "REVIEW"
                document.review_reason = (
                    "Página sem edital, datas ou quadro de cargos: aguardando documento oficial"
                )
                report.review_queue += 1
                continue
            if llm_provider is not None:
                _semantic(session, draft, found, config, llm_provider, run_id)
                verified = _locate_missing_position_fields(
                    session, draft, found, config, llm_provider
                )
                if verified.get("confirmed"):
                    record_event(
                        session,
                        run_id,
                        kind="LLM_VERIFIED",
                        message=(
                            f"{verified['confirmed']} campo(s) localizados pelo modelo e "
                            f"confirmados pelo parser em {found.url[:120]}"
                        ),
                        context=verified,
                    )
            opportunity, changes, created = upsert_opportunity(
                session,
                draft,
                source=source,
                document=document,
                version=version,
                config=config,
                today=today,
                report=report,
                run_id=run_id,
            )
            document.processing_state = "REVIEW" if draft.review_reasons else "DONE"
            document.review_reason = "; ".join(draft.review_reasons)[:500] or None
            if draft.review_reasons:
                report.review_queue += 1
            generate_alerts(
                session,
                opportunity,
                changes,
                created,
                config=config,
                report=report,
                run_id=run_id,
                trust_level=spec.trust_level,
            )
            session.flush()
        except Exception as error:  # noqa: BLE001 - one bad document never stops the source
            session.rollback()
            report.errors += 1
            logger.exception(
                "Falha ao processar documento",
                extra={"source_id": source.id, "document_id": found.url, "stage": "process"},
            )
            record_error(
                session,
                run_id,
                stage="process",
                kind=type(error).__name__,
                message=str(error),
                source_id=source.id,
                context={"url": found.url},
            )
            session.flush()


def _semantic_pending(
    session: Session, found: FoundDocument, provider: Any, config: Configuration
) -> bool:
    """Has this exact document ever been shown to this exact model and prompt?

    A document whose bytes did not change is normally skipped, which is right. But when
    the semantic layer is switched on -- or its prompt or model changes -- documents that
    were already stored have never been looked at by it, and their gaps would stay open
    forever. The cache makes this a one-off per document: once asked, it is never asked
    again for the same prompt and model.
    """
    if provider is None:
        return False
    with session.no_autoflush:
        return (
            session.execute(
                sa.select(ExtractionResult.id).where(
                    ExtractionResult.document_hash == found.content_hash,
                    ExtractionResult.prompt_version == LOCATE_PROMPT,
                    ExtractionResult.model == provider.model,
                    ExtractionResult.model_version == provider.model_version,
                )
            ).first()
            is None
        )


def _remember_extraction(
    session: Session, found: FoundDocument, provider: Any, status: str, payload: dict[str, Any]
) -> None:
    with session.no_autoflush:
        already = session.execute(
            sa.select(ExtractionResult.id).where(
                ExtractionResult.document_hash == found.content_hash,
                ExtractionResult.prompt_version == LOCATE_PROMPT,
                ExtractionResult.model == provider.model,
                ExtractionResult.model_version == provider.model_version,
            )
        ).first()
    if already:
        return  # the same document is often linked from more than one page
    session.add(
        ExtractionResult(
            document_hash=found.content_hash,
            prompt_version=LOCATE_PROMPT,
            model=provider.model,
            model_version=provider.model_version,
            status=status,
            payload=payload,
            created_at=now(),
        )
    )
    session.flush()


def _locate_missing_position_fields(
    session: Session,
    draft: OpportunityDraft,
    found: FoundDocument,
    config: Configuration,
    provider: Any,
) -> dict[str, int]:
    """Ask the model to point at the sentences the table parser never saw.

    Only runs when a cargo that is otherwise interesting still lacks a field the user
    filters on. Nothing the model says is believed: every claim goes through verify(),
    which requires the quote to exist in the document and the deterministic parser to
    re-derive the same value.
    """
    from sentinela.llm import load_prompt, parse_located
    from sentinela.verify import VERIFIERS, apply_to_position, focus_excerpt, summarize

    gaps = [
        item
        for item in draft.positions
        if not item.synthetic and any(getattr(item, name, None) is None for name in VERIFIERS)
    ]
    prompt_version = LOCATE_PROMPT
    if not gaps:
        _remember_extraction(session, found, provider, "NO_GAPS", {"positions": []})
        return {}
    with session.no_autoflush:
        cached = session.execute(
            sa.select(ExtractionResult).where(
                ExtractionResult.document_hash == found.content_hash,
                ExtractionResult.prompt_version == prompt_version,
                ExtractionResult.model == provider.model,
                ExtractionResult.model_version == provider.model_version,
            )
        ).scalar_one_or_none()
    if cached is not None:
        located = list(cached.payload.get("positions") or [])
    else:
        try:
            excerpt = focus_excerpt(found.document.text, [item.name for item in gaps])
            raw = provider.complete(load_prompt(prompt_version), excerpt)
            located = parse_located(raw)
        except Exception:  # noqa: BLE001 - provider outage must not fail the run
            # Deliberately NOT cached. A free tier answers 503 under load; writing that
            # to the cache would mark the document as "already asked" and it would never
            # be looked at again.
            return {}
        _remember_extraction(session, found, provider, "OK", {"positions": located})
    if not located:
        return {}
    by_name = {normalize_name(item["name"]): item["fields"] for item in located}
    results = []
    for position in gaps:
        wanted = normalize_name(position.name)
        claims = by_name.get(wanted)
        if claims is None:
            # A model may render the same cargo slightly differently ("Analista
            # Legislativo - Direito" vs "... – Especialidade Direito"). Containment in
            # either direction is safe here: a wrong match still has to survive the
            # quote check and the parser before anything is accepted.
            for name, fields in by_name.items():
                if name and (name in wanted or wanted in name):
                    claims = fields
                    break
        if claims:
            results += apply_to_position(
                position, claims, found.document.text, found.url, prompt_version
            )
    return summarize(results)


def normalize_name(value: str) -> str:
    from sentinela.domain import normalize

    return normalize(value)


def _semantic(
    session: Session,
    draft: OpportunityDraft,
    found: FoundDocument,
    config: Configuration,
    provider: Any,
    run_id: str | None,
) -> None:
    """Called only when deterministic extraction left an important field empty."""
    missing = [
        name
        for name in ("registration_deadline", "exam_date", "edital_number")
        if getattr(draft, name) is None
    ]
    if not missing:
        return
    prompt_version = str(config.llm.get("prompt_version") or "edital_extraction_v1")
    key = cache_key(found.content_hash, prompt_version, provider.model, provider.model_version)
    cached = session.execute(
        sa.select(ExtractionResult).where(
            ExtractionResult.document_hash == found.content_hash,
            ExtractionResult.prompt_version == prompt_version,
            ExtractionResult.model == provider.model,
            ExtractionResult.model_version == provider.model_version,
        )
    ).scalar_one_or_none()
    if cached is not None:
        from sentinela.llm import LLMResult

        fill_gaps(
            draft,
            LLMResult(
                "CACHED",
                fields=dict(cached.payload.get("fields") or {}),
                prompt_version=prompt_version,
            ),
            found.url,
        )
        return
    result = extract_semantic(provider, prompt_version, found.document.text)
    session.add(
        ExtractionResult(
            document_hash=found.content_hash,
            prompt_version=prompt_version,
            model=provider.model,
            model_version=provider.model_version,
            status=result.status,
            payload={"fields": result.fields, "detail": result.detail, "cache_key": key},
            created_at=now(),
        )
    )
    if result.usable:
        fill_gaps(draft, result, found.url)


def update_health(
    session: Session,
    source: Source,
    outcome: SourceOutcome,
    *,
    config: Configuration,
    report: RunReport,
    run_id: str,
    skipped: bool = False,
) -> str:
    state = session.get(SourceHealth, source.id)
    if state is None:
        state = SourceHealth(source_id=source.id)
        session.add(state)
        session.flush()
    if skipped:
        report.sources_skipped += 1
        return state.state

    hard_failure = outcome.status == "FAILED"
    state.total_attempts += 1
    state.last_attempt = now()
    state.last_status_code = outcome.http_status
    state.last_response_ms = outcome.response_ms
    state.last_error = outcome.error
    if outcome.etag:
        state.etag = outcome.etag
    if outcome.last_modified:
        state.last_modified = outcome.last_modified

    documents = len(outcome.documents)
    history = health.read_history(state.document_history or [])
    verdict = health.detect_drift(
        documents_found=documents,
        history=history,
        previous_fingerprint=state.content_fingerprint,
        current_fingerprint=outcome.index_fingerprint,
        expected_min=int((source.config or {}).get("expected_min_links") or 0),
        http_status=outcome.http_status,
    )
    if outcome.index_unchanged:
        verdict = health.DriftVerdict(False)  # a 304 is a healthy "nothing changed"

    if hard_failure:
        state.consecutive_failures += 1
    else:
        state.total_successes += 1
        state.consecutive_failures = 0
        state.last_success = now()
        state.document_history = health.push_history(state.document_history or [], documents)
        state.documents_last_run = documents
        values = health.read_history(state.document_history)
        state.documents_median = float(sorted(values)[len(values) // 2]) if values else None
        if outcome.index_fingerprint:
            state.content_fingerprint = outcome.index_fingerprint
    state.parser_attempts += len(outcome.documents)
    state.parser_successes += sum(1 for item in outcome.documents if item.document.usable)

    transition = health.next_state(
        current=state.state,
        success=not hard_failure,
        consecutive_failures=state.consecutive_failures,
        drift=verdict.detected,
        failure_threshold=config.monitoring.circuit_failure_threshold,
        cooldown_hours=config.monitoring.circuit_cooldown_hours,
        open_until=state.open_until,
    )
    previous_state = state.state
    state.state, state.open_until = transition.state, transition.open_until
    if verdict.detected:
        report.drifted.append(source.id)
        record_event(
            session,
            run_id,
            kind="SOURCE_DRIFT",
            severity="WARNING",
            message=f"{source.id}: {verdict.reason}",
            context={"source_id": source.id, **(verdict.evidence or {})},
        )
    if transition.state != previous_state:
        record_event(
            session,
            run_id,
            kind="SOURCE_HEALTH",
            severity="WARNING",
            message=f"{source.id}: {previous_state} -> {transition.state}. {transition.reason}",
            context={"source_id": source.id},
        )
    if transition.state in ("DEGRADED", "OPEN"):
        report.degraded.append(source.id)
    if hard_failure:
        report.sources_failed += 1
        record_error(
            session,
            run_id,
            stage="collect",
            kind="SOURCE_FAILED",
            message=outcome.error or "Falha desconhecida",
            source_id=source.id,
        )
    else:
        report.sources_successful += 1
    session.flush()
    return state.state


# ---------------------------------------------------------------- the run


def run_monitor(
    session: Session,
    config: Configuration,
    secrets: Secrets,
    *,
    kind: str = "daily",
    trigger: str = "manual",
    only: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    logger: RunLogger | None = None,
) -> RunReport:
    log = logger or RunLogger(get("run"), {})
    today = now().astimezone(config.zone).date()
    monitor = MonitorRun(kind=kind, trigger=trigger, status="RUNNING", started_at=now())
    session.add(monitor)
    session.flush()
    report = RunReport(run_id=monitor.id, started_at=monitor.started_at)
    log = log.child(run_id=monitor.id)
    log.info("Início da execução %s (%s)", kind, trigger)

    provider = build_provider(config, secrets)
    from sentinela.llm import NullProvider

    llm = None if isinstance(provider, NullProvider) else provider

    query = sa.select(Source).where(Source.enabled.is_(True)).order_by(Source.priority, Source.id)
    if only:
        query = query.where(Source.id.in_(only))
    sources = list(session.execute(query).scalars().all())
    deadline = now() + timedelta(minutes=config.monitoring.max_run_minutes)

    from sentinela.browser import BrowserFetcher

    browser = (
        BrowserFetcher(
            timeout_ms=int(config.monitoring.browser_timeout_seconds * 1000),
            max_bytes=config.monitoring.max_document_bytes,
            min_interval=config.monitoring.minimum_request_interval_seconds,
            max_pages=config.monitoring.browser_max_pages,
        )
        if config.monitoring.browser_enabled and not dry_run
        else None
    )
    with Fetcher(
        timeout=config.monitoring.source_timeout_seconds,
        max_attempts=config.monitoring.max_attempts,
        min_interval=config.monitoring.minimum_request_interval_seconds,
        max_bytes=config.monitoring.max_document_bytes,
    ) as fetcher:
        for source in sources:
            if now() > deadline:
                record_event(
                    session,
                    monitor.id,
                    kind="RUN_TIMEBOX",
                    severity="WARNING",
                    message=f"Tempo máximo de execução atingido antes de {source.id}",
                )
                report.sources_skipped += len(sources) - report.sources_attempted
                break
            report.sources_attempted += 1
            state = session.get(SourceHealth, source.id)
            skip, why = health.should_skip(
                state.state if state else "HEALTHY", state.open_until if state else None
            )
            # A person asking for a specific source is testing a fix. Refusing to try
            # would mean nobody can verify a repair until the cooldown expires.
            if skip and force:
                skip, why = False, ""
                log.info(
                    "Circuito ignorado a pedido explícito",
                    extra={"source_id": source.id, "stage": "circuit"},
                )
            if skip:
                log.info(
                    "Fonte %s ignorada: %s",
                    source.id,
                    why,
                    extra={"source_id": source.id, "stage": "circuit"},
                )
                update_health(
                    session,
                    source,
                    SourceOutcome(source.id, "SKIPPED"),
                    config=config,
                    report=report,
                    run_id=monitor.id,
                    skipped=True,
                )
                continue
            spec = spec_from_row(source)
            outcome = safe_collect(
                spec,
                fetcher,
                etag=state.etag if state else None,
                last_modified=state.last_modified if state else None,
                max_pages=config.monitoring.max_pdf_pages,
                ocr_pages=config.monitoring.max_ocr_pages,
                browser=browser,
            )
            log.info(
                "Fonte %s: %s, %d documentos",
                source.id,
                outcome.status,
                len(outcome.documents),
                extra={"source_id": source.id, "stage": "collect"},
            )
            if not dry_run:
                process_source(
                    session,
                    source,
                    outcome,
                    config=config,
                    today=today,
                    report=report,
                    run_id=monitor.id,
                    logger=log,
                    llm_provider=llm,
                )
            update_health(session, source, outcome, config=config, report=report, run_id=monitor.id)
            if outcome.rendered:
                record_event(
                    session,
                    monitor.id,
                    kind="SOURCE_RENDERED",
                    message=f"{source.id}: coleta recuperada com navegador",
                    context={"source_id": source.id},
                )
                report.rendered.append(source.id)
            session.commit()
        if browser is not None:
            browser.close()

    if not dry_run:
        generate_deadline_alerts(session, config, today, report, monitor.id)
        session.commit()
        dispatch(session, config, secrets, report, log)

    report.finished_at = now()
    report.status = "PARTIAL" if (report.sources_failed or report.errors) else "SUCCESS"
    attempted = report.sources_attempted - report.sources_skipped
    if report.sources_attempted and report.sources_skipped == report.sources_attempted:
        # Everything was deliberately skipped by the circuit breaker. Nothing failed and
        # nothing was collected; calling that FAILED would make the watchdog cry wolf.
        report.status = "SKIPPED"
    elif attempted > 0 and report.sources_successful == 0:
        report.status = "FAILED"
    monitor.status = report.status
    monitor.finished_at = report.finished_at
    monitor.sources_attempted = report.sources_attempted
    monitor.sources_successful = report.sources_successful
    monitor.sources_failed = report.sources_failed
    monitor.sources_skipped = report.sources_skipped
    monitor.documents_discovered = report.documents_discovered
    monitor.documents_changed = report.documents_changed
    monitor.opportunities_created = report.opportunities_created
    monitor.opportunities_updated = report.opportunities_updated
    monitor.notifications_sent = report.notifications_sent
    monitor.errors = report.errors
    monitor.detail = {
        "drifted": report.drifted,
        "rendered": report.rendered,
        "degraded": sorted(set(report.degraded)),
        "documents_new": report.documents_new,
        "review_queue": report.review_queue,
        "notifications_queued": report.notifications_queued,
        "dry_run": dry_run,
    }
    session.commit()
    log.info("Execução concluída: %s", report.status)
    return report


def finalize_run(
    session: Session,
    config: Configuration,
    secrets: Secrets,
    report: RunReport,
    logger: RunLogger,
) -> Any:
    """Evaluate the watchdog, queue any alert AND deliver it, in one place.

    These three steps belong together: queueing without dispatching left the "your
    monitoring is broken" alert waiting for the next run, which -- if monitoring really
    is broken -- never comes. Keeping them in one function stops a caller doing half.
    """
    from sentinela import watchdog as watchdog_module

    verdict = watchdog_module.evaluate(session, config)
    watchdog_module.notify(session, verdict, config, secrets)
    session.flush()
    dispatch(session, config, secrets, report, logger)
    return verdict


def last_successful_run(session: Session, kind: str | None = None) -> MonitorRun | None:
    query = sa.select(MonitorRun).where(MonitorRun.status.in_(["SUCCESS", "PARTIAL"]))
    if kind:
        query = query.where(MonitorRun.kind == kind)
    return session.execute(
        query.order_by(MonitorRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()


def run_is_fresh(session: Session, config: Configuration, today: date) -> tuple[bool, str]:
    """Used by the backup schedule: did the primary run already do the work today?"""
    last = last_successful_run(session)
    if last is None:
        return False, "MISSING"
    local = utc(last.started_at).astimezone(config.zone).date()
    if local != today:
        return False, "STALE"
    if last.status != "SUCCESS":
        return False, "PARTIAL"
    if last.sources_attempted and last.sources_successful < max(1, last.sources_attempted // 2):
        return False, "PARTIAL"
    return True, "SUCCESS"


def utc_schedule(local_time: str, timezone: str) -> str:
    """Convert `HH:MM` in the profile timezone to a cron expression in UTC."""
    from zoneinfo import ZoneInfo

    hour, minute = (int(piece) for piece in local_time.split(":"))
    zone = ZoneInfo(timezone)
    sample = datetime(2026, 1, 15, hour, minute, tzinfo=zone).astimezone(UTC)
    return f"{sample.minute} {sample.hour} * * *"
