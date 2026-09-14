"""reports/latest.md -- the full picture in one file, in Rio Branco local time."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from sentinela import health as health_module
from sentinela.alerts import EMPLOYMENT_LABEL, STATUS_LABEL, WORKLOAD_LABEL, day, money, period
from sentinela.config import Configuration
from sentinela.domain import now, utc
from sentinela.models import (
    Deadline,
    Document,
    ErrorLog,
    MonitorRun,
    Notification,
    Opportunity,
    Position,
    Source,
    SourceHealth,
)

SECTIONS = (
    "NOVIDADES DE HOJE",
    "INSCRIÇÕES ABERTAS",
    "COMBINAÇÕES EXCELENTES",
    "COMBINAÇÕES MUITO BOAS",
    "CARGA HORÁRIA DESCONHECIDA",
    "PRAZOS SE APROXIMANDO",
    "PROVAS AGENDADAS",
    "RETIFICAÇÕES RECENTES",
    "ANÁLISE INCOMPLETA",
    "SAÚDE DAS FONTES",
    "SAÚDE DO SISTEMA",
)


def _line(opportunity: Opportunity, position: Position) -> str:
    workload = f"{position.weekly_workload:g}h" if position.weekly_workload else "CH?"
    assignment = "Rio Branco" if position.assignment_confirmed else "lotação não confirmada"
    return (
        f"- **{position.name}** — {opportunity.institution}  \n"
        f"  {workload} · {money(position.salary)} · {assignment} · "
        f"{position.score}/100 ({position.rank}) · confiança {position.confidence}  \n"
        f"  Inscrições: {period(opportunity.registration_start, opportunity.registration_deadline)}"
        f" · Prova: {day(opportunity.exam_date)}  \n"
        f"  {EMPLOYMENT_LABEL.get(opportunity.employment_type, 'Vínculo não confirmado')} · "
        f"{STATUS_LABEL.get(opportunity.status, opportunity.status)} · "
        f"CH {WORKLOAD_LABEL.get(position.workload_classification, '?')}  \n"
        f"  {opportunity.official_edital_url or opportunity.official_institution_url or 'sem link oficial'}"
    )


def _eligible_pairs(session: Session, **filters: Any) -> list[tuple[Opportunity, Position]]:
    query = (
        sa.select(Opportunity, Position)
        .join(Position, Position.opportunity_id == Opportunity.id)
        .where(Position.eligible.is_(True), Opportunity.status.notin_(["CANCELLED", "EXPIRED"]))
        .order_by(Position.score.desc(), Opportunity.registration_deadline)
    )
    for field, value in filters.items():
        query = query.where(getattr(Position, field) == value)
    return [(row[0], row[1]) for row in session.execute(query).all()]


def _block(title: str, entries: list[str], empty: str = "_Nada nesta categoria._") -> str:
    body = "\n".join(entries) if entries else empty
    return f"## {title}\n\n{body}\n"


def build(session: Session, config: Configuration, *, today: date | None = None) -> str:
    zone = config.zone
    local_today = today or now().astimezone(zone).date()
    generated = now().astimezone(zone)
    parts: list[str] = [
        "# Sentinela AC — relatório",
        "",
        f"Gerado em **{generated:%d/%m/%Y %H:%M}** ({config.profile.timezone})  ",
        f"Perfil: {config.profile.city}/{config.profile.state} · ensino médio · "
        f"ideal ≤{config.workload.ideal_max_weekly_hours:g}h, aceitável "
        f"≤{config.workload.acceptable_max_weekly_hours:g}h",
        "",
    ]

    since = now() - timedelta(hours=26)
    new_today = list(
        session.execute(
            sa.select(Opportunity, Position)
            .join(Position, Position.opportunity_id == Opportunity.id)
            .where(Opportunity.first_seen_at >= since, Position.eligible.is_(True))
            .order_by(Position.score.desc())
        ).all()
    )
    parts.append(
        _block(
            SECTIONS[0],
            [_line(o, p) for o, p in new_today],
            "_Nenhuma oportunidade nova compatível nas últimas 24 horas._",
        )
    )

    open_now = [
        (o, p)
        for o, p in _eligible_pairs(session)
        if o.registration_start
        and o.registration_deadline
        and o.registration_start <= local_today <= o.registration_deadline
    ]
    parts.append(_block(SECTIONS[1], [_line(o, p) for o, p in open_now]))

    all_pairs = _eligible_pairs(session)
    parts.append(_block(SECTIONS[2], [_line(o, p) for o, p in all_pairs if p.rank == "EXCELLENT"]))
    parts.append(_block(SECTIONS[3], [_line(o, p) for o, p in all_pairs if p.rank == "VERY GOOD"]))
    parts.append(
        _block(
            SECTIONS[4],
            [_line(o, p) for o, p in _eligible_pairs(session, workload_classification="UNKNOWN")],
            "_Nenhuma oportunidade pendente de confirmação de carga horária._",
        )
    )

    horizon = local_today + timedelta(days=30)
    deadlines = session.execute(
        sa.select(Deadline, Opportunity)
        .join(Opportunity, Opportunity.id == Deadline.opportunity_id)
        .where(
            Deadline.active.is_(True),
            Deadline.due_date >= local_today,
            Deadline.due_date <= horizon,
            Opportunity.eligible.is_(True),
        )
        .order_by(Deadline.due_date)
    ).all()
    parts.append(
        _block(
            SECTIONS[5],
            [
                f"- {day(d.due_date)} (em {(d.due_date - local_today).days}d) — {d.description} · "
                f"{o.institution} — {o.name}"
                for d, o in deadlines
            ],
        )
    )

    exams = [(o, p) for o, p in all_pairs if o.exam_date and o.exam_date >= local_today]
    parts.append(
        _block(
            SECTIONS[6], [f"- {day(o.exam_date)} — {p.name} · {o.institution}" for o, p in exams]
        )
    )

    rectifications = (
        session.execute(
            sa.select(Document)
            .where(
                Document.document_type.in_(
                    ["RECTIFICATION", "EXTENSION", "REOPENING", "SUSPENSION", "CANCELLATION"]
                ),
                Document.last_seen_at >= now() - timedelta(days=14),
            )
            .order_by(Document.last_seen_at.desc())
            .limit(25)
        )
        .scalars()
        .all()
    )
    parts.append(
        _block(
            SECTIONS[7],
            [
                f"- {document.document_type} — {(document.title or document.url)[:110]}  \n  {document.url}"
                for document in rectifications
            ],
        )
    )

    review = (
        session.execute(
            sa.select(Document)
            .where(Document.processing_state == "REVIEW")
            .order_by(Document.last_seen_at.desc())
            .limit(30)
        )
        .scalars()
        .all()
    )
    parts.append(
        _block(
            SECTIONS[8],
            [
                f"- {(document.review_reason or 'motivo não registrado')[:120]}  \n  {document.url}"
                for document in review
            ],
            "_Nenhum documento na fila de revisão._",
        )
    )

    health_rows = session.execute(
        sa.select(Source, SourceHealth)
        .join(SourceHealth, SourceHealth.source_id == Source.id)
        .order_by(Source.priority, Source.id)
    ).all()
    states = [state.state for _, state in health_rows]
    summary = health_module.summarize(states)
    lines = [
        "| Fonte | Estado | Última coleta OK | Docs | Parser | Observação |",
        "|---|---|---|---|---|---|",
    ]
    for source, state in health_rows:
        rate = health_module.parser_rate(state.parser_successes, state.parser_attempts)
        last_ok = (
            utc(state.last_success).astimezone(zone).strftime("%d/%m %H:%M")
            if state.last_success
            else "nunca"
        )
        note = (state.last_error or source.limitations or "")[:70]
        lines.append(
            f"| {source.id} | {state.state} | {last_ok} | {state.documents_last_run or 0} | "
            f"{rate:.0%} | {note} |"
        )
    parts.append(
        f"## {SECTIONS[9]}\n\n"
        + "\n".join(lines)
        + "\n\n"
        + f"Total {len(health_rows)} · saudáveis {summary['healthy']} · "
        f"degradadas {summary['degraded']} · circuito aberto {summary['open']} · "
        f"desativadas {summary['disabled']}\n"
    )

    last_run = session.execute(
        sa.select(MonitorRun).order_by(MonitorRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    pending = session.execute(
        sa.select(Notification.status, sa.func.count()).group_by(Notification.status)
    ).all()
    recent_errors = (
        session.execute(
            sa.select(ErrorLog)
            .where(ErrorLog.created_at >= now() - timedelta(days=2))
            .order_by(ErrorLog.created_at.desc())
            .limit(12)
        )
        .scalars()
        .all()
    )
    system: list[str] = []
    if last_run:
        system.append(
            f"- Última execução: **{last_run.status}** em "
            f"{utc(last_run.started_at).astimezone(zone):%d/%m/%Y %H:%M} · "
            f"{last_run.sources_successful}/{last_run.sources_attempted} fontes · "
            f"{last_run.documents_discovered} documentos · "
            f"{last_run.opportunities_created} novas oportunidades · "
            f"{last_run.errors} erros"
        )
    system.append(
        "- Notificações: "
        + (", ".join(f"{status} {count}" for status, count in pending) or "nenhuma registrada")
    )
    total_opportunities = session.execute(
        sa.select(sa.func.count()).select_from(Opportunity)
    ).scalar_one()
    eligible_count = session.execute(
        sa.select(sa.func.count()).select_from(Opportunity).where(Opportunity.eligible.is_(True))
    ).scalar_one()
    system.append(
        f"- Oportunidades no banco: {total_opportunities} · compatíveis: {eligible_count}"
    )
    if recent_errors:
        system.append("- Erros recentes:")
        system += [
            f"  - `{error.stage}/{error.kind}` {(error.source_id or '')} — {error.message[:90]}"
            for error in recent_errors
        ]
    parts.append(_block(SECTIONS[10], system))

    parts.append(
        "---\n\n_Todo dado acima vem de documento oficial coletado e versionado. "
        "Campos ausentes aparecem como não informados: o Sentinela nunca preenche o que "
        "não leu._\n"
    )
    return "\n".join(parts)


def write(
    session: Session,
    config: Configuration,
    directory: str | Path | None = None,
    *,
    today: date | None = None,
) -> Path:
    target = Path(directory or config.storage.get("reports_directory", "reports"))
    target.mkdir(parents=True, exist_ok=True)
    path = target / "latest.md"
    path.write_text(build(session, config, today=today), encoding="utf-8")
    return path
