"""Read-only local dashboard; personal notes stay in the user's browser.

Explicit projections prevent credentials, notification bodies and source config from
being accidentally exposed. No collector, LLM or notification is invoked here.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from sentinela.alerts import EDUCATION_LABEL, EMPLOYMENT_LABEL, STATUS_LABEL
from sentinela.config import Configuration
from sentinela.domain import now, utc
from sentinela.models import (
    Deadline,
    Document,
    MonitorRun,
    Opportunity,
    OpportunityVersion,
    Position,
    Source,
    SourceHealth,
)
from sentinela.watchdog import evaluate

INACTIVE = frozenset({"CANCELLED", "SUSPENDED", "EXPIRED", "HOMOLOGATED"})
WEB = Path(__file__).with_name("web")
logger = logging.getLogger(__name__)


def scalar(value: Any) -> Any:
    if isinstance(value, datetime):
        return utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def project(row: Any, fields: str) -> dict[str, Any]:
    return {name: scalar(getattr(row, name)) for name in fields.split()}


def registration_state(opportunity: Opportunity, today: date) -> str:
    # Stored status can lag between monitoring runs. Dates constrain OPEN, while a
    # missing date never proves that registration is open.
    if opportunity.status in INACTIVE:
        return "INACTIVE"
    if opportunity.registration_deadline and opportunity.registration_deadline < today:
        return "CLOSED"
    if opportunity.registration_start and opportunity.registration_start > today:
        return "FUTURE"
    if opportunity.status == "REGISTRATION_CLOSED":
        return "CLOSED"
    if opportunity.registration_start and opportunity.registration_deadline:
        return "OPEN"
    return "UNKNOWN"


def opportunity_data(row: Opportunity, today: date, *, details: bool = False) -> dict[str, Any]:
    result = project(
        row,
        "id name institution edital_number status employment_type employment_regime "
        "publication_date registration_start registration_deadline exam_date exam_location "
        "application_fee fee_exemption fee_exemption_deadline payment_deadline "
        "organizing_board selection_stages validity official_edital_url "
        "official_application_url official_institution_url organizing_board_url score "
        "confidence eligible needs_review reasons first_seen_at last_seen_at version",
    )
    result.update(
        active=row.status not in INACTIVE,
        status_label=STATUS_LABEL.get(row.status, row.status),
        employment_label=EMPLOYMENT_LABEL.get(row.employment_type, row.employment_type),
        registration_state=registration_state(row, today),
    )
    result["positions"] = []
    for position in sorted(row.positions, key=lambda p: (-p.score, p.name, p.id)):
        item = project(
            position,
            "id name education qualification_category additional_qualifications requirements "
            "vacancies reserve_list reserve_count assignment_location assignment_confirmed "
            "possible_assignment_locations salary benefits weekly_workload daily_workload "
            "workload_classification score confidence eligible needs_review reasons synthetic",
        )
        # The panel must never print a stored code at a person: "high_school" is
        # not an answer to "what schooling does it require?".
        item["education_label"] = (
            EDUCATION_LABEL.get(position.education, position.education)
            if position.education
            else None
        )
        if details:
            item["evidence"] = position.evidence
        result["positions"].append(item)
    if details:
        result.update(
            evidence=row.evidence, conflicts=row.conflicts, evidence_sources=row.evidence_sources
        )
    return result


def deadline_data(
    session: Session, today: date, opportunity_id: str | None = None
) -> list[dict[str, Any]]:
    query = (
        sa.select(Deadline, Opportunity)
        .join(Opportunity, Opportunity.id == Deadline.opportunity_id)
        .where(
            Deadline.active.is_(True),
            Deadline.due_date >= today,
            Opportunity.status.notin_(INACTIVE),
        )
        .order_by(Deadline.due_date, Deadline.id)
    )
    if opportunity_id:
        query = query.where(Opportunity.id == opportunity_id)
    else:
        # A stale opportunity aggregate must not create a deadline for zero real cargos.
        query = query.where(
            Opportunity.positions.any(
                sa.and_(Position.eligible.is_(True), Position.synthetic.is_(False))
            )
        )
    return [
        {
            **project(
                deadline, "id opportunity_id kind description due_date source_url updated_at"
            ),
            "institution": opportunity.institution,
        }
        for deadline, opportunity in session.execute(query).all()
    ]


def dashboard_data(session: Session, config: Configuration) -> dict[str, Any]:
    moment = now()
    today = moment.astimezone(config.zone).date()
    rows = session.scalars(
        sa.select(Opportunity).order_by(Opportunity.score.desc(), Opportunity.id)
    ).all()
    opportunities = [opportunity_data(row, today) for row in rows]
    sources = []
    for source, health in session.execute(
        sa.select(Source, SourceHealth)
        .outerjoin(SourceHealth, SourceHealth.source_id == Source.id)
        .order_by(Source.priority, Source.name)
    ):
        sources.append(
            {
                **project(source, "id name official trust_status enabled base_url"),
                "state": health.state
                if health and source.enabled
                else ("UNKNOWN" if source.enabled else "DISABLED"),
                **(
                    project(
                        health, "last_success last_attempt consecutive_failures last_status_code"
                    )
                    if health
                    else {
                        "last_success": None,
                        "last_attempt": None,
                        "consecutive_failures": 0,
                        "last_status_code": None,
                    }
                ),
            }
        )
    runs = [
        project(
            run,
            "id kind status started_at finished_at sources_attempted sources_successful sources_failed documents_discovered opportunities_created opportunities_updated",
        )
        for run in session.scalars(
            sa.select(MonitorRun).order_by(MonitorRun.started_at.desc()).limit(15)
        )
    ]
    verdict = evaluate(session, config, moment)
    active_positions = [
        p for o in opportunities if o["active"] for p in o["positions"] if not p["synthetic"]
    ]
    review_count = session.scalar(
        sa.select(sa.func.count())
        .select_from(Document)
        .where(Document.processing_state == "REVIEW")
    )
    return {
        "generated_at": moment.isoformat(),
        "today": today.isoformat(),
        "timezone": config.profile.timezone,
        "profile": {
            "city": config.profile.city,
            "state": config.profile.state,
            "ideal_hours": config.workload.ideal_max_weekly_hours,
            "max_hours": config.workload.acceptable_max_weekly_hours,
        },
        "summary": {
            "opportunities": len(opportunities),
            "positions": sum(len(o["positions"]) for o in opportunities),
            "eligible_positions": sum(bool(p["eligible"]) for p in active_positions),
            "review_positions": sum(bool(p["needs_review"]) for p in active_positions),
            "documents_review": review_count,
            "sources_total": len(sources),
            "sources_healthy": sum(s["state"] == "HEALTHY" and s["enabled"] for s in sources),
        },
        "monitor": {
            "severity": verdict.severity,
            "title": verdict.title,
            "last_run": scalar(verdict.last_success),
            "schedule": config.monitoring.primary_local_time,
        },
        "opportunities": opportunities,
        "deadlines": deadline_data(session, today),
        "sources": sources,
        "runs": runs,
    }


def detail_data(
    session: Session, config: Configuration, opportunity_id: str
) -> dict[str, Any] | None:
    row = session.get(Opportunity, opportunity_id)
    if not row:
        return None
    history = []
    for version in session.scalars(
        sa.select(OpportunityVersion)
        .where(OpportunityVersion.opportunity_id == row.id)
        .order_by(OpportunityVersion.version.desc())
    ):
        item = project(version, "version detected_at source_url document_id document_version_id")
        item["changes"] = [
            {**change, "old": change.get("old_value"), "new": change.get("new_value")}
            for change in version.changes
        ]
        history.append(item)
    return {
        "opportunity": opportunity_data(row, now().astimezone(config.zone).date(), details=True),
        "history": history,
    }


def _ics_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _fold_ics(line: str) -> str:
    # RFC 5545 folds at 75 OCTETS, without splitting UTF-8 characters.
    parts: list[str] = []
    current = ""
    for char in line:
        if len((current + char).encode("utf-8")) > 75:
            parts.append(current)
            current = " "
        current += char
    parts.append(current)
    return "\r\n".join(parts)


def calendar_data(
    session: Session, config: Configuration, opportunity_id: str | None = None
) -> str:
    today = now().astimezone(config.zone).date()
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Sentinela AC//Agenda//PT-BR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Sentinela AC",
    ]
    for item in deadline_data(session, today, opportunity_id):
        due = date.fromisoformat(item["due_date"])
        updated = datetime.fromisoformat(item["updated_at"])
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{item['id']}@sentinela-ac.local",
                f"DTSTAMP:{updated:%Y%m%dT%H%M%SZ}",
                f"LAST-MODIFIED:{updated:%Y%m%dT%H%M%SZ}",
                f"DTSTART;VALUE=DATE:{due:%Y%m%d}",
                f"DTEND;VALUE=DATE:{due + timedelta(days=1):%Y%m%d}",
                "SUMMARY:"
                + _ics_text(f"{item['description'] or item['kind']} — {item['institution']}"),
                "DESCRIPTION:"
                + _ics_text(
                    "Confira o horário limite e eventuais retificações no edital oficial. "
                    + (item["source_url"] or "Fonte não informada.")
                ),
            ]
        )
        url = item["source_url"]
        if url and urlsplit(url).scheme in {"https", "http"}:
            lines.append("URL:" + _ics_text(url))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold_ics(line) for line in lines) + "\r\n"


def make_server(engine: Engine, config: Configuration, port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server: ThreadingHTTPServer
        server_version = "SentinelaLocal/1"

        def log_message(self, format: str, *args: Any) -> None:
            # Never log untrusted request URLs or database exception text.
            pass

        def respond(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
            )
            if content_type.startswith("text/calendar"):
                self.send_header(
                    "Content-Disposition", 'attachment; filename="sentinela-agenda.ics"'
                )
            self.end_headers()
            self.wfile.write(body)

        def json(self, status: int, value: Any) -> None:
            self.respond(
                status,
                json.dumps(value, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )

        def do_GET(self) -> None:
            # Loopback binding + exact Host + Origin checks prevent DNS rebinding and
            # cross-site browser access to a service that has no public authentication.
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != host or self.headers.get("Origin") not in (
                None,
                f"http://{host}",
            ):
                self.json(403, {"error": "Acesso permitido apenas pela origem local do painel."})
                return
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.json(403, {"error": "Origem externa recusada."})
                return
            parsed = urlsplit(self.path)
            static = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                "/assets/space-grotesk.woff2": ("assets/space-grotesk.woff2", "font/woff2"),
            }
            if parsed.path in static:
                filename, mime = static[parsed.path]
                path = WEB / filename
                if path.is_file():
                    self.respond(200, path.read_bytes(), mime)
                else:
                    self.json(404, {"error": "Arquivo do painel não encontrado."})
                return
            if parsed.path == "/api/health":
                self.json(200, {"service": "sentinela-dashboard", "version": 1})
                return
            opportunity_id = None
            if parsed.path.startswith("/api/opportunities/"):
                try:
                    opportunity_id = str(uuid.UUID(parsed.path.rsplit("/", 1)[-1]))
                except ValueError:
                    self.json(404, {"error": "Concurso não encontrado."})
                    return
            elif parsed.path not in {"/api/dashboard", "/api/calendar.ics"}:
                self.json(404, {"error": "Página não encontrada."})
                return
            try:
                with Session(engine) as session:
                    if engine.dialect.name == "postgresql":
                        session.execute(sa.text("SET TRANSACTION READ ONLY"))
                        session.execute(sa.text("SET LOCAL statement_timeout = '15000'"))
                    if parsed.path == "/api/dashboard":
                        self.json(200, dashboard_data(session, config))
                    elif parsed.path == "/api/calendar.ics":
                        selected = parse_qs(parsed.query).get("opportunity_id", [None])[0]
                        if selected:
                            try:
                                selected = str(uuid.UUID(selected))
                            except ValueError:
                                self.json(400, {"error": "Identificador de concurso inválido."})
                                return
                            if session.get(Opportunity, selected) is None:
                                self.json(404, {"error": "Concurso não encontrado."})
                                return
                        self.respond(
                            200,
                            calendar_data(session, config, selected).encode(),
                            "text/calendar; charset=utf-8",
                        )
                    elif opportunity_id:
                        data = detail_data(session, config, opportunity_id)
                        self.json(
                            200 if data else 404, data or {"error": "Concurso não encontrado."}
                        )
            except (sa.exc.SQLAlchemyError, OSError, ValueError) as error:
                logger.warning("Dashboard request failed: %s", type(error).__name__)
                self.json(
                    503,
                    {"error": "Não foi possível consultar o banco. Tente atualizar em instantes."},
                )

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(engine: Engine, config: Configuration, port: int = 8765) -> None:
    server = make_server(engine, config, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.dispose()
