"""End-to-end pipeline behaviour against a real database session."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from sentinela import tracking
from sentinela import watchdog as watchdog_module
from sentinela.collector import FoundDocument, SourceOutcome
from sentinela.config import Configuration, Secrets
from sentinela.domain import Evidence, OpportunityDraft, PositionDraft, digest
from sentinela.extract import ExtractedDocument
from sentinela.models import (
    Deadline,
    Document,
    DocumentVersion,
    MonitorRun,
    Notification,
    Opportunity,
    OpportunityVersion,
    Position,
    RawSnapshot,
    Source,
)
from sentinela.pipeline import (
    RunReport,
    generate_deadline_alerts,
    persist_document,
    process_source,
    run_is_fresh,
    upsert_opportunity,
    utc_schedule,
)

TODAY = date(2026, 9, 14)
# Notification.run_id is a real UUID column, so the stub id must be one too.
RUN_ID = "00000000-0000-4000-8000-000000000001"
URL = "https://exemplo.riobranco.ac.gov.br/edital-01-2026.pdf"


def found(
    body: bytes,
    *,
    url: str = URL,
    text: str = "",
    title: str = "Edital 01/2026",
    usable: bool = True,
) -> FoundDocument:
    document = ExtractedDocument(
        text=text or "x" * 200,
        title=title,
        method="pdf_pymupdf",
        status="OK" if usable else "NO_TEXT_LAYER",
        page_count=4,
    )
    return FoundDocument(
        url=url,
        title=title,
        media_type="pdf",
        content=body,
        document=document,
        http_status=200,
        etag=None,
        last_modified=None,
        content_hash=digest(body),
        fetched_at=datetime.now(UTC),
    )


def draft(**overrides) -> OpportunityDraft:
    base = OpportunityDraft(
        institution="Prefeitura Municipal de Rio Branco",
        name="Concurso público para nível médio",
        edital_number="01/2026",
        publication_date=date(2026, 9, 11),
        employment_type="PERMANENT",
        registration_start=date(2026, 9, 11),
        registration_deadline=date(2026, 10, 13),
        exam_date=date(2026, 11, 22),
        official_edital_url=URL,
        official_institution_url=URL,
    )
    positions = overrides.pop("positions", None)
    for key, value in overrides.items():
        setattr(base, key, value)
    base.positions = (
        positions
        if positions is not None
        else [
            PositionDraft(
                name="Agente Administrativo",
                education="high_school",
                qualification_category="HIGH SCHOOL ONLY",
                vacancies=2,
                salary=Decimal("2500.00"),
                weekly_workload=20.0,
                assignment_location="Rio Branco",
                possible_assignment_locations=["Rio Branco"],
                assignment_confirmed=True,
                requirements="Certificado de conclusão de curso de nível médio.",
                evidence={
                    "education": Evidence(
                        value="high_school",
                        status="FOUND",
                        source_url=URL,
                        extraction_method="table_parser",
                        confidence=0.95,
                        raw_evidence="nível médio",
                    ),
                    "requirements": Evidence(
                        value="...",
                        status="FOUND",
                        source_url=URL,
                        extraction_method="table_parser",
                        confidence=0.95,
                        raw_evidence="certificado",
                    ),
                    "weekly_workload": Evidence(
                        value=20.0,
                        status="FOUND",
                        source_url=URL,
                        extraction_method="table_parser",
                        confidence=0.95,
                        raw_evidence="20 horas semanais",
                    ),
                    "assignment_location": Evidence(
                        value="Rio Branco",
                        status="FOUND",
                        source_url=URL,
                        extraction_method="table_parser",
                        confidence=0.95,
                        raw_evidence="lotação",
                    ),
                },
            )
        ]
    )
    return base


def store(
    session: Session,
    source: Source,
    item: OpportunityDraft,
    config: Configuration,
    report: RunReport,
    body: bytes = b"pdf-1",
) -> tuple[Opportunity, list, bool]:
    document, version, _ = persist_document(session, source.id, found(body), report)
    result = upsert_opportunity(
        session,
        item,
        source=source,
        document=document,
        version=version,
        config=config,
        today=TODAY,
        report=report,
        run_id=None,
    )
    session.commit()
    return result


# ---------------------------------------------------------------- documents


def test_unchanged_content_does_not_create_a_second_version(
    session: Session, source: Source
) -> None:
    report = RunReport(run_id=RUN_ID)
    body = b"%PDF-1.7 conteudo"
    document, version, changed = persist_document(session, source.id, found(body), report)
    session.commit()
    assert changed is True

    _, again, changed_again = persist_document(session, source.id, found(body), report)
    session.commit()
    assert changed_again is False
    assert again.id == version.id
    assert (
        session.execute(sa.select(sa.func.count()).select_from(DocumentVersion)).scalar_one() == 1
    )


def test_changed_content_appends_a_version_and_keeps_the_old_one(
    session: Session, source: Source
) -> None:
    report = RunReport(run_id=RUN_ID)
    persist_document(session, source.id, found(b"versao 1"), report)
    session.commit()
    document, version, changed = persist_document(session, source.id, found(b"versao 2"), report)
    session.commit()
    assert changed is True
    assert (
        session.execute(sa.select(sa.func.count()).select_from(DocumentVersion)).scalar_one() == 2
    )
    assert document.current_version_id == version.id
    snapshots = session.execute(sa.select(sa.func.count()).select_from(RawSnapshot)).scalar_one()
    assert snapshots == 2  # both texts stay auditable


# ---------------------------------------------------------------- opportunities


def test_first_ingest_creates_opportunity_positions_and_deadlines(
    session: Session, source: Source, config: Configuration
) -> None:
    report = RunReport(run_id=RUN_ID)
    opportunity, changes, created = store(session, source, draft(), config, report)
    assert created is True
    assert opportunity.status == "REGISTRATION_OPEN"
    assert opportunity.eligible is True
    assert opportunity.rank == "EXCELLENT"
    assert opportunity.workload_classification == "PERFECT"
    assert len(opportunity.positions) == 1
    kinds = {row.kind for row in session.execute(sa.select(Deadline)).scalars()}
    assert kinds == {"registration", "exam"}
    assert (
        session.execute(sa.select(sa.func.count()).select_from(OpportunityVersion)).scalar_one()
        == 1
    )


def test_reingesting_the_same_document_is_a_no_op(
    session: Session, source: Source, config: Configuration
) -> None:
    report = RunReport(run_id=RUN_ID)
    store(session, source, draft(), config, report)
    before = session.execute(sa.select(Opportunity.version)).scalar_one()
    opportunity, changes, created = store(session, source, draft(), config, report)
    assert created is False
    assert changes == []
    assert opportunity.version == before + 1  # a version row is always appended
    assert session.execute(sa.select(sa.func.count()).select_from(Opportunity)).scalar_one() == 1


def test_a_deadline_extension_is_detected_and_both_values_survive(
    session: Session, source: Source, config: Configuration
) -> None:
    report = RunReport(run_id=RUN_ID)
    store(session, source, draft(), config, report)
    _, changes, _ = store(
        session,
        source,
        draft(registration_deadline=date(2026, 10, 20)),
        config,
        report,
        body=b"pdf-2",
    )
    fields = {item["field"]: item for item in changes}
    assert fields["registration_deadline"]["old_value"] == "13/10/2026"
    assert fields["registration_deadline"]["new_value"] == "20/10/2026"
    assert fields["registration_deadline"]["alertable"] is True

    versions = (
        session.execute(sa.select(OpportunityVersion).order_by(OpportunityVersion.version))
        .scalars()
        .all()
    )
    assert versions[0].snapshot["registration_deadline"] == "2026-10-13"
    assert versions[-1].snapshot["registration_deadline"] == "2026-10-20"


def test_four_sources_describing_one_certame_produce_one_opportunity(
    session: Session, source: Source, config: Configuration
) -> None:
    report = RunReport(run_id=RUN_ID)
    aggregator = Source(
        id="agregador",
        name="Agregador",
        institution="Agregador",
        base_url="https://agregador.com.br/",
        official=False,
        trust_level=3,
        priority=9,
        trust_status="TRUSTED",
        config={"allowed_hosts": ["agregador.com.br"]},
    )
    session.add(aggregator)
    session.commit()

    store(session, source, draft(), config, report, body=b"oficial")
    store(
        session, source, draft(name="CONCURSO PÚBLICO — PMRB 2026"), config, report, body=b"diario"
    )
    document, version, _ = persist_document(
        session, aggregator.id, found(b"agregador", url="https://agregador.com.br/rb"), report
    )
    upsert_opportunity(
        session,
        draft(name="Concurso Prefeitura RB"),
        source=aggregator,
        document=document,
        version=version,
        config=config,
        today=TODAY,
        report=report,
        run_id=None,
    )
    session.commit()

    assert session.execute(sa.select(sa.func.count()).select_from(Opportunity)).scalar_one() == 1
    opportunity = session.execute(sa.select(Opportunity)).scalar_one()
    assert set(opportunity.evidence_sources) == {source.id, aggregator.id}
    assert opportunity.best_trust_level == 1


def test_a_weaker_document_never_overwrites_a_stronger_one(
    session: Session, source: Source, config: Configuration
) -> None:
    # Regression: a listing page with no cargo table replaced the salary and the name
    # that the opening edital had established.
    report = RunReport(run_id=RUN_ID)
    store(session, source, draft(), config, report, body=b"edital")
    placeholder = PositionDraft(name="Concurso público", synthetic=True, education=None)
    store(
        session,
        source,
        draft(name="Página de notícias", positions=[placeholder], registration_deadline=None),
        config,
        report,
        body=b"noticia",
    )
    opportunity = session.execute(sa.select(Opportunity)).scalar_one()
    assert opportunity.name == "Concurso público para nível médio"
    assert opportunity.registration_deadline == date(2026, 10, 13)
    assert [item.name for item in opportunity.positions] == ["Agente Administrativo"]


def test_status_never_runs_backwards(
    session: Session, source: Source, config: Configuration
) -> None:
    report = RunReport(run_id=RUN_ID)
    store(session, source, draft(), config, report, body=b"edital")
    store(
        session,
        source,
        draft(
            registration_start=None,
            registration_deadline=None,
            exam_date=None,
            positions=[PositionDraft(name="Concurso público", synthetic=True)],
        ),
        config,
        report,
        body=b"noticia",
    )
    opportunity = session.execute(sa.select(Opportunity)).scalar_one()
    assert opportunity.status == "REGISTRATION_OPEN"


def test_cancellation_document_sets_a_terminal_status(
    session: Session, source: Source, config: Configuration
) -> None:
    report = RunReport(run_id=RUN_ID)
    store(session, source, draft(), config, report, body=b"edital")
    store(session, source, draft(document_type="CANCELLATION"), config, report, body=b"cancela")
    assert session.execute(sa.select(Opportunity)).scalar_one().status == "CANCELLED"


def test_exam_city_alone_never_makes_an_opportunity_eligible(
    session: Session, source: Source, config: Configuration
) -> None:
    report = RunReport(run_id=RUN_ID)
    remote = PositionDraft(
        name="Técnico Judiciário",
        education="high_school",
        qualification_category="HIGH SCHOOL ONLY",
        weekly_workload=30.0,
        assignment_location=None,
        possible_assignment_locations=[],
        assignment_confirmed=False,
    )
    opportunity, _, _ = store(
        session, source, draft(exam_location="Rio Branco", positions=[remote]), config, report
    )
    assert opportunity.eligible is False
    assert all(item.primary_alert is False for item in opportunity.positions)


# ---------------------------------------------------------------- notifications


def test_notifications_are_idempotent_across_runs(
    session: Session, source: Source, config: Configuration
) -> None:
    report = RunReport(run_id=RUN_ID)
    outcome = SourceOutcome(source.id, "OK", [found(b"edital-real")])
    for _ in range(3):
        process_source(
            session,
            source,
            outcome,
            config=config,
            today=TODAY,
            report=report,
            run_id=None,
            logger=_logger(),
        )
        session.commit()
    assert session.execute(sa.select(sa.func.count()).select_from(Notification)).scalar_one() <= 1


def test_deadline_alerts_fire_once_per_threshold(
    session: Session, source: Source, config: Configuration
) -> None:
    report = RunReport(run_id=RUN_ID)
    store(session, source, draft(), config, report)
    # 13/10/2026 minus 7 days.
    for _ in range(3):
        generate_deadline_alerts(session, config, date(2026, 10, 6), report, RUN_ID)
        session.commit()
    rows = (
        session.execute(sa.select(Notification).where(Notification.category == "DEADLINE"))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert "7 dias" in rows[0].body

    generate_deadline_alerts(session, config, date(2026, 10, 10), report, RUN_ID)
    session.commit()
    rows = (
        session.execute(sa.select(Notification).where(Notification.category == "DEADLINE"))
        .scalars()
        .all()
    )
    assert len(rows) == 2  # the 3-day threshold is a different alert


def test_no_alert_on_a_day_with_nothing_to_say(
    session: Session, source: Source, config: Configuration
) -> None:
    report = RunReport(run_id=RUN_ID)
    store(session, source, draft(), config, report)
    generate_deadline_alerts(session, config, date(2026, 9, 20), report, RUN_ID)
    session.commit()
    assert (
        session.execute(
            sa.select(sa.func.count())
            .select_from(Notification)
            .where(Notification.category == "DEADLINE")
        ).scalar_one()
        == 0
    )


# ---------------------------------------------------------------- failure handling


def test_unreadable_document_goes_to_review_and_never_becomes_an_opportunity(
    session: Session, source: Source, config: Configuration
) -> None:
    report = RunReport(run_id=RUN_ID)
    broken = found(b"%PDF-1.4 scan", usable=False)
    process_source(
        session,
        source,
        SourceOutcome(source.id, "OK", [broken]),
        config=config,
        today=TODAY,
        report=report,
        run_id=None,
        logger=_logger(),
    )
    session.commit()
    document = session.execute(sa.select(Document)).scalar_one()
    assert document.processing_state == "REVIEW"
    assert session.execute(sa.select(sa.func.count()).select_from(Opportunity)).scalar_one() == 0
    assert report.review_queue == 1


def test_a_partially_completed_run_is_not_counted_as_fresh(
    session: Session, config: Configuration
) -> None:
    from sentinela.domain import now

    session.add(
        MonitorRun(
            kind="daily",
            status="PARTIAL",
            started_at=now(),
            sources_attempted=30,
            sources_successful=2,
        )
    )
    session.commit()
    fresh, status = run_is_fresh(session, config, now().astimezone(config.zone).date())
    assert fresh is False
    assert status == "PARTIAL"


def test_a_successful_run_today_lets_the_backup_exit_early(
    session: Session, config: Configuration
) -> None:
    from sentinela.domain import now

    session.add(
        MonitorRun(
            kind="daily",
            status="SUCCESS",
            started_at=now(),
            sources_attempted=30,
            sources_successful=29,
        )
    )
    session.commit()
    fresh, status = run_is_fresh(session, config, now().astimezone(config.zone).date())
    assert (fresh, status) == (True, "SUCCESS")


def test_a_stale_run_makes_the_backup_take_over(session: Session, config: Configuration) -> None:
    from sentinela.domain import now

    session.add(
        MonitorRun(
            kind="daily",
            status="SUCCESS",
            started_at=now() - timedelta(days=2),
            sources_attempted=30,
            sources_successful=30,
        )
    )
    session.commit()
    fresh, status = run_is_fresh(session, config, now().astimezone(config.zone).date())
    assert (fresh, status) == (False, "STALE")


# ---------------------------------------------------------------- watchdog


def test_watchdog_is_critical_when_nothing_ever_ran(
    session: Session, config: Configuration
) -> None:
    verdict = watchdog_module.evaluate(session, config)
    assert verdict.severity == "CRITICAL"
    assert verdict.healthy is False


def test_watchdog_is_critical_after_the_silence_window(
    session: Session, config: Configuration
) -> None:
    from sentinela.domain import now

    session.add(MonitorRun(kind="daily", status="SUCCESS", started_at=now() - timedelta(hours=40)))
    session.commit()
    verdict = watchdog_module.evaluate(session, config)
    assert verdict.severity == "CRITICAL"
    assert "Sem monitoramento" in verdict.title


def test_watchdog_alert_is_queued_once_per_day(
    session: Session, config: Configuration, secrets: Secrets
) -> None:
    verdict = watchdog_module.evaluate(session, config)
    first = watchdog_module.notify(session, verdict, config, secrets)
    session.commit()
    second = watchdog_module.notify(session, verdict, config, secrets)
    session.commit()
    assert first == 1
    assert second == 0


def test_watchdog_stays_quiet_when_everything_is_fine(
    session: Session, config: Configuration, secrets: Secrets
) -> None:
    from sentinela.domain import now

    session.add(MonitorRun(kind="daily", status="SUCCESS", started_at=now() - timedelta(hours=2)))
    session.commit()
    verdict = watchdog_module.evaluate(session, config)
    assert verdict.severity == "OK"
    assert watchdog_module.notify(session, verdict, config, secrets) == 0


# ---------------------------------------------------------------- schedule


@pytest.mark.parametrize(
    "local,expected",
    [
        ("07:17", "17 12 * * *"),  # Rio Branco is UTC-5 all year
        ("08:17", "17 13 * * *"),
        ("23:30", "30 4 * * *"),
    ],
)
def test_schedule_conversion_to_utc(local: str, expected: str) -> None:
    assert utc_schedule(local, "America/Rio_Branco") == expected


def _logger():
    from sentinela.logging import RunLogger, get

    return RunLogger(get("test"), {})


def test_alert_queued_before_credentials_exist_is_delivered_later(
    session: Session, source: Source, secrets: Secrets
) -> None:
    """Regression: the first production run happened before the Telegram secrets were
    registered. The alert was marked BLOCKED -- a terminal state -- so it was never
    retried, and idempotency meant it could never be regenerated either. The user would
    simply never have received the one opportunity that mattered."""
    from sentinela.logging import RunLogger, get
    from sentinela.pipeline import dispatch, queue_notification

    config = Configuration(notifications={"telegram": True, "markdown": False})
    report = RunReport(run_id=RUN_ID)
    logger = RunLogger(get("test"), {})

    queue_notification(
        session,
        key="k",
        category="NEW_OPPORTUNITY",
        body="alerta",
        channels=["telegram"],
        subject="Agente Legislativo",
    )
    session.commit()

    # No credentials yet.
    dispatch(session, config, secrets, report, logger)
    session.commit()
    row = session.execute(sa.select(Notification)).scalar_one()
    assert row.status == "UNCONFIGURED"
    assert report.notifications_sent == 0

    # Credentials arrive; the same row must now be delivered.
    from pydantic import SecretStr

    configured = Secrets(
        _env_file=None,
        telegram_bot_token=SecretStr("1234567890:token"),
        telegram_chat_id=SecretStr("42"),
    )
    sent: list[str] = []

    class Fake:
        name = "telegram"

        def send(self, key: str, message: str):
            from sentinela.notifications import DeliveryResult

            sent.append(key)
            return DeliveryResult("SENT", external_id="99")

    import sentinela.notifications as notifications_module

    original = notifications_module.make_notifiers
    notifications_module.make_notifiers = lambda *_: [Fake()]
    try:
        dispatch(session, config, configured, report, logger)
        session.commit()
    finally:
        notifications_module.make_notifiers = original

    session.refresh(row)
    assert row.status == "SENT"
    assert sent == ["telegram:k"]
    assert report.notifications_sent == 1


def test_warning_watchdog_speaks_once_per_situation_not_once_per_day(
    session: Session, source: Source, config: Configuration, secrets: Secrets
) -> None:
    """Regression: a WARNING was keyed on the calendar date, so the same nine
    permanently-broken portals produced an 'infrastructure problem' message every single
    morning. The per-source GitHub issues already carry that detail."""
    from sentinela.domain import now
    from sentinela.models import SourceHealth

    session.add(MonitorRun(kind="daily", status="SUCCESS", started_at=now()))
    state = session.get(SourceHealth, source.id)
    state.state = "OPEN"
    session.commit()

    verdict = watchdog_module.evaluate(session, config)
    assert verdict.severity == "WARNING"
    assert watchdog_module.notify(session, verdict, config, secrets) == 1
    session.commit()

    # Same situation tomorrow: silence.
    assert watchdog_module.notify(session, verdict, config, secrets) == 0
    session.commit()

    # The situation gets worse: that is news.
    worse = watchdog_module.WatchdogVerdict(
        True,
        "WARNING",
        verdict.title,
        verdict.detail,
        verdict.actions,
        verdict.last_success,
        verdict.hours_since,
        fingerprint="outro-conjunto",
    )
    assert watchdog_module.notify(session, worse, config, secrets) == 1


def test_critical_watchdog_keeps_repeating_daily(
    session: Session, config: Configuration, secrets: Secrets
) -> None:
    """A stopped monitor is urgent and unresolved: it must be said again every day."""
    verdict = watchdog_module.evaluate(session, config)
    assert verdict.severity == "CRITICAL"
    assert watchdog_module.notify(session, verdict, config, secrets) == 1
    session.commit()
    assert watchdog_module.notify(session, verdict, config, secrets) == 0


def test_a_run_where_everything_was_skipped_is_not_a_failure(
    session: Session, source: Source, config: Configuration, secrets: Secrets
) -> None:
    """Regression: a run whose sources were all deliberately skipped by the circuit
    breaker reported FAILED. Nothing failed and nothing was collected -- calling that a
    failure would make the watchdog cry wolf about a healthy system."""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from sentinela.models import SourceHealth
    from sentinela.pipeline import run_monitor

    state = session.get(SourceHealth, source.id)
    state.state = "OPEN"
    state.open_until = _datetime(2099, 1, 1, tzinfo=_UTC)
    session.commit()

    result = run_monitor(session, config, secrets, kind="manual", only=[source.id])
    assert result.sources_skipped == 1
    assert result.sources_failed == 0
    assert result.status == "SKIPPED"


def test_force_lets_an_operator_test_a_repair_before_the_cooldown(
    session: Session, source: Source, config: Configuration, secrets: Secrets
) -> None:
    """Regression: the runbook says to use `source-check` to reproduce a broken source,
    but the circuit breaker silently refused, so a fix could not be verified for hours."""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from sentinela.models import SourceHealth
    from sentinela.pipeline import run_monitor

    state = session.get(SourceHealth, source.id)
    state.state = "OPEN"
    state.open_until = _datetime(2099, 1, 1, tzinfo=_UTC)
    session.commit()

    result = run_monitor(
        session, config, secrets, kind="manual", only=[source.id], force=True, dry_run=True
    )
    assert result.sources_skipped == 0
    assert result.sources_attempted == 1


def test_watchdog_alert_is_not_left_queued(
    session: Session, config: Configuration, secrets: Secrets
) -> None:
    """Regression: the alert was queued but never dispatched, so 'your monitoring is
    broken' waited for the NEXT run -- which, if monitoring really is broken, never
    arrives. finalize_run keeps queueing and delivering together."""
    from sentinela.logging import RunLogger, get
    from sentinela.pipeline import finalize_run

    report = RunReport(run_id=RUN_ID)
    verdict = finalize_run(session, config, secrets, report, RunLogger(get("test"), {}))
    session.commit()

    assert verdict.severity == "CRITICAL"
    rows = (
        session.execute(sa.select(Notification).where(Notification.category == "SYSTEM"))
        .scalars()
        .all()
    )
    assert rows, "o watchdog deveria ter gerado alerta"
    assert all(row.status == "SENT" for row in rows)
    assert report.notifications_sent == len(rows)


def test_a_transient_provider_failure_is_never_cached(
    session: Session, source: Source, config: Configuration
) -> None:
    """Regression: a free-tier 503 was written to the extraction cache, marking the
    document as 'already asked'. It would then never be looked at again, so its gaps
    would stay open forever because of one bad minute."""
    import sqlalchemy as _sa

    from sentinela.models import ExtractionResult
    from sentinela.pipeline import _locate_missing_position_fields, _semantic_pending

    asked: list[str] = []

    class Broken:
        name, model, model_version = "broken", "gemini-3.5-flash", "gemini-3.5-flash"

        def complete(self, prompt: str, document: str) -> str:
            asked.append(document)
            raise httpx.HTTPStatusError(
                "503",
                request=httpx.Request("POST", "https://x"),
                response=httpx.Response(503),
            )

    draft_item = draft()
    draft_item.positions[0].weekly_workload = None
    draft_item.positions[0].salary = None
    found_document = found(b"conteudo-com-lacuna")

    result = _locate_missing_position_fields(session, draft_item, found_document, config, Broken())
    session.commit()

    assert asked, "o provedor precisa ter sido realmente consultado"
    assert result == {}
    assert (
        session.execute(_sa.select(_sa.func.count()).select_from(ExtractionResult)).scalar_one()
        == 0
    )
    # Still pending, so the next run will try again instead of giving up for good.
    assert _semantic_pending(session, found_document, Broken(), config) is True


def test_the_same_document_twice_in_one_run_does_not_break_it(
    session: Session, source: Source, config: Configuration
) -> None:
    """Regression: the same PDF is routinely linked from more than one page. Checking the
    extraction cache autoflushed the row just queued for it, turning the second sighting
    into a unique-key violation that killed the document."""
    from sentinela.pipeline import _locate_missing_position_fields, _semantic_pending

    class Quiet:
        name, model, model_version = "quiet", "gemini-3.5-flash", "gemini-3.5-flash"

        def complete(self, prompt: str, document: str) -> str:
            return '{"positions": []}'

    provider = Quiet()
    same = found(b"mesmo-pdf-linkado-duas-vezes")
    complete_draft = draft()  # every field already known: no gaps

    for _ in range(3):
        assert (
            _locate_missing_position_fields(session, complete_draft, same, config, provider) == {}
        )
    session.commit()
    assert _semantic_pending(session, same, provider, config) is False


def test_disagreeing_sources_are_recorded_not_silently_overwritten(
    session: Session, source: Source, config: Configuration
) -> None:
    """Regression: the merge did `if current is None or better: setattr(...)`, so when a
    second source stated a different exam date the old value vanished without trace. The
    design forbids exactly that: record the conflict and preserve both values."""
    report = RunReport(run_id=RUN_ID)
    store(session, source, draft(), config, report, body=b"edital-oficial")

    weaker = Source(
        id="agregador-conflito",
        name="Agregador",
        institution="Agregador",
        base_url="https://agregador.com.br/",
        official=False,
        trust_level=3,
        priority=9,
        trust_status="TRUSTED",
        config={"allowed_hosts": ["agregador.com.br"]},
    )
    session.add(weaker)
    session.commit()

    document, version, _ = persist_document(
        session, weaker.id, found(b"agregador", url="https://agregador.com.br/rb"), report
    )
    upsert_opportunity(
        session,
        draft(exam_date=date(2027, 1, 30)),
        source=weaker,
        document=document,
        version=version,
        config=config,
        today=TODAY,
        report=report,
        run_id=None,
    )
    session.commit()

    opportunity = session.execute(sa.select(Opportunity)).scalar_one()
    # The authoritative value stands...
    assert opportunity.exam_date == date(2026, 11, 22)
    # ...and the disagreement is on the record, with both sides preserved.
    clash = [item for item in opportunity.conflicts if item["field"] == "exam_date"]
    assert len(clash) == 1
    assert "2026-11-22" in clash[0]["kept"]
    assert "2027-01-30" in clash[0]["rejected"]
    assert clash[0]["rejected_source"] == weaker.id


def test_identical_values_from_two_sources_are_not_a_conflict(
    session: Session, source: Source, config: Configuration
) -> None:
    report = RunReport(run_id=RUN_ID)
    store(session, source, draft(), config, report, body=b"um")
    store(session, source, draft(), config, report, body=b"dois")
    opportunity = session.execute(sa.select(Opportunity)).scalar_one()
    assert opportunity.conflicts == []


def test_every_source_attempt_leaves_a_run_row(
    session: Session, source: Source, config: Configuration, secrets: Secrets
) -> None:
    """Regression: source_runs was declared, migrated and never written. source_health
    only knows the present, so 'when did this source start failing?' was unanswerable."""
    from sentinela.models import SourceRun
    from sentinela.pipeline import run_monitor

    run_monitor(session, config, secrets, kind="manual", only=[source.id], dry_run=True)
    session.commit()

    row = session.execute(sa.select(SourceRun)).scalar_one()
    assert row.source_id == source.id
    assert row.status in ("OK", "EMPTY", "FAILED")
    assert row.started_at is not None and row.finished_at is not None


def test_a_source_leaving_the_cooling_period_passes_through_recovering(
    session: Session, source: Source, config: Configuration, secrets: Secrets
) -> None:
    """Regression: entering_recovery() existed but was never called, so the state jumped
    OPEN -> HEALTHY and RECOVERING was unreachable dead code."""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from sentinela.models import SourceHealth, SystemEvent
    from sentinela.pipeline import run_monitor

    state = session.get(SourceHealth, source.id)
    state.state = "OPEN"
    state.open_until = _datetime(2020, 1, 1, tzinfo=_UTC)  # cooling period long over
    session.commit()

    run_monitor(session, config, secrets, kind="manual", only=[source.id], dry_run=True)
    session.commit()

    events = (
        session.execute(sa.select(SystemEvent).where(SystemEvent.kind == "SOURCE_HEALTH"))
        .scalars()
        .all()
    )
    assert any("RECOVERING" in item.message for item in events)


def test_url_and_acronym_differences_are_not_conflicts(
    session: Session, source: Source, config: Configuration
) -> None:
    """Regression: the first version of conflict detection flagged every document's own
    URL and treated 'IDIB' vs 'Instituto de Desenvolvimento Institucional Brasileiro' as
    a disagreement. Real conflicts would have been buried under that noise."""
    report = RunReport(run_id=RUN_ID)
    store(session, source, draft(organizing_board="IDIB"), config, report, body=b"a")
    store(
        session,
        source,
        draft(
            organizing_board="Instituto de Desenvolvimento Institucional Brasileiro",
            official_institution_url="https://outra.riobranco.ac.gov.br/pagina",
            official_application_url="http://www.idib.org.br",
        ),
        config,
        report,
        body=b"b",
    )
    session.commit()

    opportunity = session.execute(sa.select(Opportunity)).scalar_one()
    noisy = {"organizing_board", "official_institution_url", "official_application_url"}
    assert not [item for item in opportunity.conflicts if item["field"] in noisy]


def test_the_per_run_document_budget_is_enforced(
    session: Session, source: Source, config: Configuration, secrets: Secrets
) -> None:
    """Regression: monitoring.max_documents_per_run was declared in config.yaml and never
    read. The safety limit simply did not exist -- 33 sources at 8 documents each can
    reach 264 while the file says 160."""
    import re as _re

    from sentinela.collector import collect
    from sentinela.domain import SourceSpec
    from sentinela.fetch import Fetcher

    page = b"".join(
        f'<a href="https://x.gov.br/edital-{n}.pdf">Edital {n}</a>'.encode() for n in range(20)
    )

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=page, headers={"content-type": "text/html"})

    spec = SourceSpec(
        id="muitos",
        name="m",
        institution="i",
        base_url="https://x.gov.br/",
        allowed_hosts=["x.gov.br"],
        max_documents=20,
        link_pattern=_re.escape(".pdf"),
    )
    with Fetcher(
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        min_interval=0,
        sleeper=lambda _: None,
    ) as fetcher:
        unlimited = collect(spec, fetcher)
        limited = collect(spec, fetcher, budget=3)
    assert len(unlimited.documents) > 3
    assert len(limited.documents) == 3


def test_uncertain_delivery_is_held_by_default_and_retried_only_on_request(
    session: Session, source: Source, secrets: Secrets
) -> None:
    """Regression: notifications.retry_uncertain_delivery was declared and never read, so
    what the file presented as a choice was hardcoded."""
    from sentinela.logging import RunLogger, get
    from sentinela.notifications import DeliveryResult
    from sentinela.pipeline import dispatch, queue_notification

    class Ambiguous:
        name = "telegram"

        def send(self, key: str, message: str) -> DeliveryResult:
            return DeliveryResult("UNCERTAIN", error="sem confirmacao")

    import sentinela.notifications as notifications_module

    original = notifications_module.make_notifiers
    notifications_module.make_notifiers = lambda *_: [Ambiguous()]
    try:
        for retry, expected in ((False, "UNCERTAIN"), (True, "RETRY")):
            settings = Configuration(
                notifications={
                    "telegram": True,
                    "markdown": False,
                    "retry_uncertain_delivery": retry,
                }
            )
            queue_notification(
                session,
                key=f"k{retry}",
                category="NEW_OPPORTUNITY",
                body="x",
                channels=["telegram"],
            )
            session.commit()
            dispatch(session, settings, secrets, RunReport(run_id=RUN_ID), RunLogger(get("t"), {}))
            session.commit()
            row = session.execute(
                sa.select(Notification).where(Notification.idempotency_key == f"telegram:k{retry}")
            ).scalar_one()
            assert row.status == expected
    finally:
        notifications_module.make_notifiers = original


def test_a_run_records_how_much_storage_is_left(
    session: Session, source: Source, config: Configuration, secrets: Secrets
) -> None:
    """Regression: storage.warning_size_mb existed because the free tier stops accepting
    writes at its ceiling, and nothing ever checked it. A monitor that dies because the
    disk filled is precisely the silent death this project exists to prevent."""
    from sentinela.pipeline import run_monitor

    watched = Configuration(
        notifications=dict(config.notifications),
        storage={"reports_directory": "reports", "warning_size_mb": 350},
    )
    result = run_monitor(session, watched, secrets, kind="manual", only=[source.id], dry_run=True)
    session.commit()

    if session.get_bind().dialect.name == "sqlite":
        # The question has no answer there; the run must simply not blow up.
        assert "database_mb" not in result.detail
    else:
        assert result.detail["database_mb"] > 0


def test_a_parser_improvement_reaches_documents_already_stored(
    session: Session, source: Source, config: Configuration
) -> None:
    """Regression: parser_version was written on every document version and never once
    compared. Unchanged documents were skipped, so every parser improvement only ever
    reached editais published afterwards while everything already stored kept the gaps
    the old parser left. The spec asks for exactly this reprocessing trigger."""
    from sentinela.collector import SourceOutcome
    from sentinela.domain import PARSER_VERSION
    from sentinela.logging import RunLogger, get
    from sentinela.models import DocumentVersion
    from sentinela.pipeline import process_source

    report = RunReport(run_id=RUN_ID)
    document_found = found(b"%PDF edital ja armazenado")
    outcome = SourceOutcome(source.id, "OK", [document_found])
    logger = RunLogger(get("test"), {})

    process_source(
        session,
        source,
        outcome,
        config=config,
        today=TODAY,
        report=report,
        run_id=None,
        logger=logger,
    )
    session.commit()

    version = session.execute(sa.select(DocumentVersion)).scalar_one()
    version.parser_version = "versao-antiga"
    document = session.execute(sa.select(Document)).scalar_one()
    document.processing_state = "DONE"
    session.commit()

    process_source(
        session,
        source,
        outcome,
        config=config,
        today=TODAY,
        report=report,
        run_id=None,
        logger=logger,
    )
    session.commit()

    session.refresh(version)
    assert version.parser_version == PARSER_VERSION, "o documento antigo nao foi reprocessado"


def test_changed_metadata_reprocesses_identical_bytes(session: Session, source: Source) -> None:
    """Regression: metadata_hash was written on every version and never compared. The
    spec lists changed metadata as its own reprocessing trigger, and it is a real one: a
    PDF republished under a corrected title is the same bytes in a different document."""
    report = RunReport(run_id=RUN_ID)
    body = b"%PDF bytes identicos"

    _, first, changed = persist_document(session, source.id, found(body, title="Edital 01"), report)
    session.commit()
    assert changed is True

    _, again, unchanged = persist_document(
        session, source.id, found(body, title="Edital 01"), report
    )
    session.commit()
    assert unchanged is False, "mesmos bytes e mesmo titulo nao devem reprocessar"

    _, same_row, rechanged = persist_document(
        session, source.id, found(body, title="Edital 01 (RETIFICADO)"), report
    )
    session.commit()
    assert rechanged is True, "titulo novo sobre os mesmos bytes precisa reinterpretar"
    assert same_row.id == first.id  # still one version: the content did not change


def test_a_dismissed_certame_stops_producing_deadline_alerts(
    session: Session, source: Source, config: Configuration
) -> None:
    """Continuar lembrando o prazo de algo que ele recusou é o ruído que faz uma pessoa
    parar de ler os alertas."""
    report = RunReport(run_id=RUN_ID)
    store(session, source, draft(), config, report)
    for position in session.scalars(sa.select(Position).where(Position.eligible.is_(True))):
        tracking.mark(session, position.id, "DISMISSED")
    session.commit()
    generate_deadline_alerts(session, config, date(2026, 10, 6), report, RUN_ID)
    session.commit()
    assert (
        session.execute(
            sa.select(sa.func.count())
            .select_from(Notification)
            .where(Notification.category == "DEADLINE")
        ).scalar_one()
        == 0
    )


def test_a_deadline_alert_tells_him_what_he_already_marked(
    session: Session, source: Source, config: Configuration
) -> None:
    report = RunReport(run_id=RUN_ID)
    store(session, source, draft(), config, report)
    position = session.scalars(sa.select(Position).where(Position.eligible.is_(True))).first()
    assert position is not None
    tracking.mark(session, position.id, "REGISTERED")
    session.commit()
    generate_deadline_alerts(session, config, date(2026, 10, 6), report, RUN_ID)
    session.commit()
    body = session.scalars(
        sa.select(Notification).where(Notification.category == "DEADLINE")
    ).first()
    assert body is not None
    assert "Você marcou" in body.body
    assert "inscrição feita" in body.body
