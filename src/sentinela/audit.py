"""Weekly deep audit and automatic GitHub issue creation.

The daily run optimises for not missing today's edital. This one optimises for catching
what the daily run got wrong: stale sources, abandoned review items, unknown workloads,
expired certames and links that stopped working.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx
import sqlalchemy as sa
from sqlalchemy.orm import Session

from sentinela.config import Configuration, Secrets
from sentinela.domain import now, utc
from sentinela.fetch import Fetcher, canonical_url, host_allowed
from sentinela.logging import RunLogger, get
from sentinela.models import (
    Document,
    Opportunity,
    Source,
    SourceHealth,
    SystemEvent,
)
from sentinela.registry import register_candidate, spec_from_row

ISSUE_MARKER = "<!-- sentinela-source-degraded -->"
# Hosts worth following when a source page links out to a new official portal.
_OFFICIAL_SUFFIXES = (".gov.br", ".jus.br", ".leg.br", ".mp.br", ".def.br", ".tc.br", ".edu.br")


@dataclass
class AuditReport:
    sources_revalidated: int = 0
    sources_broken: list[str] = field(default_factory=list)
    urls_changed: list[dict[str, str]] = field(default_factory=list)
    documents_retried: int = 0
    documents_recovered: int = 0
    workloads_resolved: int = 0
    statuses_expired: int = 0
    candidates_discovered: list[str] = field(default_factory=list)
    links_broken: list[str] = field(default_factory=list)
    conflicts_open: int = 0
    issues_created: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def revalidate_sources(
    session: Session, fetcher: Fetcher, report: AuditReport, logger: RunLogger
) -> None:
    """Confirm every registered entry point still resolves, and record redirects."""
    for source in session.execute(sa.select(Source).where(Source.enabled.is_(True))).scalars():
        spec = spec_from_row(source)
        target = spec.validation_url or spec.base_url
        response = fetcher.get(target)
        report.sources_revalidated += 1
        state = session.get(SourceHealth, source.id)
        if not (response.ok or response.unchanged):
            report.sources_broken.append(source.id)
            if state is not None:
                state.last_error = response.error or f"HTTP {response.status}"
            session.add(
                SystemEvent(
                    kind="SOURCE_REVALIDATION",
                    severity="WARNING",
                    message=f"{source.id}: entry point inacessível ({response.error or response.status})",
                    context={"source_id": source.id, "url": target},
                    created_at=now(),
                )
            )
            continue
        final = canonical_url(response.url)
        if final and final != canonical_url(target):
            if host_allowed(final, spec.allowed_hosts):
                report.urls_changed.append({"source": source.id, "from": target, "to": final})
                session.add(
                    SystemEvent(
                        kind="SOURCE_URL_CHANGED",
                        severity="INFO",
                        message=f"{source.id}: {target} -> {final}",
                        context={"source_id": source.id},
                        created_at=now(),
                    )
                )
            else:
                # A redirect off the allow-list is a migration we must not follow blindly.
                report.sources_broken.append(source.id)
                session.add(
                    SystemEvent(
                        kind="SOURCE_MIGRATED",
                        severity="WARNING",
                        message=f"{source.id} redireciona para host fora da allow-list: {final}",
                        context={"source_id": source.id, "url": final},
                        created_at=now(),
                    )
                )
        logger.info("Revalidada %s", source.id, extra={"source_id": source.id, "stage": "audit"})
    session.flush()


def retry_incomplete(
    session: Session,
    config: Configuration,
    secrets: Secrets,
    report: AuditReport,
    logger: RunLogger,
) -> None:
    """Re-run the full pipeline over documents stuck in REVIEW."""
    from sentinela.collector import FoundDocument
    from sentinela.extract import extract
    from sentinela.pipeline import RunReport, process_source

    stuck = (
        session.execute(
            sa.select(Document)
            .where(Document.processing_state == "REVIEW", Document.attempts < 6)
            .order_by(Document.last_seen_at.desc())
            .limit(40)
        )
        .scalars()
        .all()
    )
    if not stuck:
        return
    today = now().astimezone(config.zone).date()
    stub = RunReport(run_id="audit")
    with Fetcher(
        timeout=config.monitoring.source_timeout_seconds,
        min_interval=config.monitoring.minimum_request_interval_seconds,
        max_bytes=config.monitoring.max_document_bytes,
    ) as fetcher:
        for document in stuck:
            source = session.get(Source, document.source_id)
            if source is None:
                continue
            response = fetcher.get(document.url)
            report.documents_retried += 1
            if not response.ok or not response.content:
                continue
            spec = spec_from_row(source)
            extracted = extract(
                response.content,
                response.url,
                response.media_type,
                allowed_hosts=spec.allowed_hosts,
                max_pages=config.monitoring.max_pdf_pages,
                ocr_pages=config.monitoring.max_ocr_pages,
            )
            from sentinela.collector import SourceOutcome
            from sentinela.domain import digest

            found = FoundDocument(
                url=document.url,
                title=document.title or "",
                media_type=response.media_type,
                content=response.content,
                document=extracted,
                http_status=response.status,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                content_hash=digest(response.content),
                fetched_at=now(),
            )
            process_source(
                session,
                source,
                SourceOutcome(source.id, "OK", [found]),
                config=config,
                today=today,
                report=stub,
                run_id=None,
                logger=logger,
                llm_provider=None,
            )
            session.refresh(document)
            if document.processing_state == "DONE":
                report.documents_recovered += 1
            session.commit()
    report.notes.append(
        f"Reprocessados {report.documents_retried}, recuperados {report.documents_recovered}"
    )


def resolve_statuses(session: Session, config: Configuration, report: AuditReport) -> None:
    """Expire certames whose windows have clearly passed, so reports stay truthful."""
    today: date = now().astimezone(config.zone).date()
    grace = today - timedelta(days=config.monitoring.freshness_days)
    rows = (
        session.execute(
            sa.select(Opportunity).where(
                Opportunity.status.notin_(["EXPIRED", "CANCELLED", "HOMOLOGATED"])
            )
        )
        .scalars()
        .all()
    )
    for opportunity in rows:
        latest = max(
            [
                value
                for value in (
                    opportunity.exam_date,
                    opportunity.registration_deadline,
                    opportunity.publication_date,
                )
                if value
            ],
            default=None,
        )
        if latest and latest < grace:
            opportunity.status = "EXPIRED"
            report.statuses_expired += 1
        elif (
            opportunity.registration_deadline
            and opportunity.registration_deadline < today
            and opportunity.status == "REGISTRATION_OPEN"
        ):
            opportunity.status = (
                "EXAM_SCHEDULED"
                if opportunity.exam_date and opportunity.exam_date >= today
                else "REGISTRATION_CLOSED"
            )
    session.flush()


def count_unknown_workloads(session: Session) -> int:
    from sentinela.models import Position

    return session.execute(
        sa.select(sa.func.count())
        .select_from(Position)
        .where(Position.eligible.is_(True), Position.weekly_workload.is_(None))
    ).scalar_one()


def verify_links(session: Session, fetcher: Fetcher, report: AuditReport, limit: int = 60) -> None:
    """An official edital URL that stopped resolving must not stay in an alert."""
    rows = (
        session.execute(
            sa.select(Opportunity)
            .where(
                Opportunity.eligible.is_(True),
                Opportunity.official_edital_url.is_not(None),
                Opportunity.status.notin_(["EXPIRED", "CANCELLED"]),
            )
            .limit(limit)
        )
        .scalars()
        .all()
    )
    for opportunity in rows:
        response = fetcher.get(opportunity.official_edital_url or "")
        if not (response.ok or response.unchanged):
            report.links_broken.append(opportunity.official_edital_url or "")
            session.add(
                SystemEvent(
                    kind="LINK_BROKEN",
                    severity="WARNING",
                    message=f"Edital inacessível: {opportunity.official_edital_url}",
                    context={"opportunity_id": opportunity.id},
                    created_at=now(),
                )
            )
    session.flush()


def discover_sources(
    session: Session, fetcher: Fetcher, report: AuditReport, limit: int = 8
) -> None:
    """Look for new official portals linked from the ones we already trust."""
    from sentinela.extract import extract

    known_hosts: set[str] = set()
    for source in session.execute(sa.select(Source)).scalars():
        known_hosts.update((source.config or {}).get("allowed_hosts") or [])
    seeds = (
        session.execute(
            sa.select(Source)
            .where(
                Source.enabled.is_(True),
                Source.trust_status == "TRUSTED",
                Source.official.is_(True),
            )
            .order_by(Source.priority)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    for source in seeds:
        response = fetcher.get(source.base_url)
        if not response.ok:
            continue
        # allowed_hosts empty on purpose: discovery is exactly about leaving the allow-list.
        document = extract(response.content, response.url, response.media_type, allowed_hosts=[])
        for url, label in document.links[:400]:
            from urllib.parse import urlparse

            host = urlparse(url).netloc.lower()
            if not host or host in known_hosts:
                continue
            if not host.endswith(_OFFICIAL_SUFFIXES):
                continue
            from sentinela.domain import normalize

            if not any(
                word in normalize(f"{url} {label}")
                for word in ("concurso", "seletivo", "edital", "carreira", "servidor")
            ):
                continue
            candidate_id = f"cand-{host.replace('.', '-')}"[:64]
            if register_candidate(
                session,
                source_id=candidate_id,
                name=f"Candidata: {host}",
                institution=host,
                base_url=f"{urlparse(url).scheme}://{host}/",
                discovered_from=source.id,
                allowed_hosts=[host],
            ):
                known_hosts.add(host)
                report.candidates_discovered.append(candidate_id)
    session.flush()


def count_conflicts(session: Session) -> int:
    rows = (
        session.execute(sa.select(Opportunity.conflicts).where(Opportunity.conflicts.is_not(None)))
        .scalars()
        .all()
    )
    return sum(1 for entry in rows if entry)


def run_deep_audit(
    session: Session, config: Configuration, secrets: Secrets, logger: RunLogger | None = None
) -> AuditReport:
    log = logger or RunLogger(get("audit"), {"stage": "audit"})
    report = AuditReport()
    with Fetcher(
        timeout=config.monitoring.source_timeout_seconds,
        min_interval=config.monitoring.minimum_request_interval_seconds,
    ) as fetcher:
        revalidate_sources(session, fetcher, report, log)
        session.commit()
        verify_links(session, fetcher, report)
        session.commit()
        discover_sources(session, fetcher, report)
        session.commit()
    retry_incomplete(session, config, secrets, report, log)
    resolve_statuses(session, config, report)
    report.workloads_resolved = count_unknown_workloads(session)
    report.conflicts_open = count_conflicts(session)
    session.add(
        SystemEvent(
            kind="DEEP_AUDIT",
            severity="INFO",
            message=(
                f"Auditoria semanal: {report.sources_revalidated} fontes revalidadas, "
                f"{len(report.sources_broken)} com problema, "
                f"{report.documents_recovered} documentos recuperados"
            ),
            context={
                "broken": report.sources_broken,
                "candidates": report.candidates_discovered,
                "links_broken": report.links_broken[:20],
            },
            created_at=now(),
        )
    )
    session.commit()
    return report


# ---------------------------------------------------------------- GitHub issues


def _api(secrets: Secrets) -> tuple[str, dict[str, str]] | None:
    token = secrets.github_token.get_secret_value() or os.environ.get("GITHUB_TOKEN", "")
    repository = secrets.github_repository or os.environ.get("GITHUB_REPOSITORY", "")
    if not (token and repository):
        return None
    return (
        f"https://api.github.com/repos/{repository}/issues",
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def open_issue_numbers(secrets: Secrets, client: httpx.Client | None = None) -> dict[str, int]:
    """Map source id -> open issue number, so we never duplicate an unresolved report."""
    api = _api(secrets)
    if api is None:
        return {}
    url, headers = api
    owned = client is None
    active = client or httpx.Client(timeout=20)
    try:
        response = active.get(url, headers=headers, params={"state": "open", "per_page": 100})
        response.raise_for_status()
        issues = response.json()
    except (httpx.HTTPError, ValueError):
        return {}
    finally:
        if owned:
            active.close()
    found: dict[str, int] = {}
    for issue in issues if isinstance(issues, list) else []:
        body = str(issue.get("body") or "")
        if ISSUE_MARKER not in body:
            continue
        for line in body.splitlines():
            if line.startswith("source_id:"):
                found[line.split(":", 1)[1].strip()] = int(issue.get("number") or 0)
    return found


def issue_body(source: Source, state: SourceHealth, config: Configuration) -> str:
    last_ok = (
        utc(state.last_success).astimezone(config.zone).strftime("%d/%m/%Y %H:%M")
        if state.last_success
        else "nunca"
    )
    suspected = (
        "Mudança de layout ou de URL no portal"
        if state.state == "DEGRADED"
        else "Portal indisponível ou bloqueando requisições automatizadas"
    )
    return "\n".join(
        [
            ISSUE_MARKER,
            f"source_id: {source.id}",
            "",
            f"**Fonte:** {source.name} ({source.institution})",
            f"**URL:** {source.base_url}",
            f"**Estado de saúde:** {state.state}",
            f"**Última coleta bem-sucedida:** {last_ok}",
            f"**Falhas consecutivas:** {state.consecutive_failures}",
            f"**Último HTTP:** {state.last_status_code or 'sem resposta'}",
            f"**Parser:** {source.adapter} (sucesso "
            f"{state.parser_successes}/{state.parser_attempts or 0})",
            f"**Erro (sanitizado):** {(state.last_error or 'não registrado')[:300]}",
            f"**Causa provável:** {suspected}",
            "",
            "**Limitações conhecidas da fonte:**",
            source.limitations or "nenhuma registrada",
            "",
            "---",
            "Aberta automaticamente pelo Sentinela AC. Nenhuma credencial ou configuração "
            "sensível é incluída neste relatório.",
        ]
    )


def report_degraded_sources(
    session: Session, config: Configuration, secrets: Secrets, client: httpx.Client | None = None
) -> list[int]:
    """Open one issue per unresolved degraded source, never a duplicate."""
    api = _api(secrets)
    if api is None:
        return []
    url, headers = api
    threshold = config.monitoring.issue_failure_threshold
    rows = session.execute(
        sa.select(Source, SourceHealth)
        .join(SourceHealth, SourceHealth.source_id == Source.id)
        .where(
            Source.enabled.is_(True),
            sa.or_(SourceHealth.consecutive_failures >= threshold, SourceHealth.state == "OPEN"),
        )
    ).all()
    if not rows:
        return []
    existing = open_issue_numbers(secrets, client)
    created: list[int] = []
    owned = client is None
    active = client or httpx.Client(timeout=20)
    try:
        for source, state in rows:
            if source.id in existing:
                state.issue_number = existing[source.id]
                continue
            payload = {
                "title": f"[Fonte degradada] {source.name}",
                "body": issue_body(source, state, config),
                "labels": ["fonte-degradada", "automatico"],
            }
            try:
                response = active.post(url, headers=headers, content=json.dumps(payload))
                response.raise_for_status()
                number = int(response.json().get("number") or 0)
            except (httpx.HTTPError, ValueError, TypeError):
                continue
            state.issue_number = number
            created.append(number)
        session.flush()
    finally:
        if owned:
            active.close()
    return created
