"""Notification engine: what to say, when to say it, and how to never say it twice.

Idempotency is structural. Every alert derives a key from stable facts (opportunity id,
category, the value being announced). The key is a unique column in the outbox, so a
retried run, a duplicated schedule or a crash between send and commit cannot duplicate it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sentinela.domain import digest, normalize

TELEGRAM_LIMIT = 4096
TRUNCATION_NOTE = "[mensagem truncada]"
WORKLOAD_LABEL = {
    "PERFECT": "PERFEITA (≤20h)",
    "GOOD": "BOA (21–30h)",
    "UNKNOWN": "NÃO CONFIRMADA",
    "OUTSIDE TARGET": "FORA DO ALVO (>30h)",
}
EMPLOYMENT_LABEL = {
    "PERMANENT": "Cargo público efetivo",
    "PUBLIC_EMPLOYMENT": "Emprego público (CLT)",
    "SELECTION": "Processo seletivo",
    "TEMPORARY": "CONTRATO TEMPORÁRIO",
    "UNKNOWN": "Vínculo não confirmado",
}
QUALIFICATION_LABEL = {
    "HIGH SCHOOL ONLY": "Nenhuma além do ensino médio",
    "HIGH SCHOOL + TECHNICAL QUALIFICATION": "Ensino médio + curso técnico",
    "HIGH SCHOOL + PROFESSIONAL REGISTRATION": "Ensino médio + registro profissional",
    "HIGH SCHOOL + DRIVER/LICENSE REQUIREMENT": "Ensino médio + CNH",
    "OTHER ADDITIONAL REQUIREMENT": "Outra exigência adicional",
    "UNKNOWN": "Não confirmada",
}
EDUCATION_LABEL = {
    "primary": "Ensino fundamental",
    "high_school": "Ensino médio",
    "higher_education": "Ensino superior",
}
STATUS_LABEL = {
    "EXPECTED": "Previsto",
    "AUTHORIZED": "Autorizado",
    "BOARD_SELECTED": "Banca definida",
    "EDITAL_PUBLISHED": "Edital publicado",
    "REGISTRATION_OPEN": "Inscrições abertas",
    "REGISTRATION_CLOSED": "Inscrições encerradas",
    "EXAM_SCHEDULED": "Prova marcada",
    "IN_PROGRESS": "Em andamento",
    "HOMOLOGATED": "Homologado",
    "SUSPENDED": "SUSPENSO",
    "CANCELLED": "CANCELADO",
    "EXPIRED": "Expirado",
}
CATEGORY_TITLE = {
    "NEW_OPPORTUNITY": "NOVA OPORTUNIDADE — RIO BRANCO",
    "REGISTRATION_OPEN": "INSCRIÇÕES ABERTAS",
    "CHANGE": "ALTERAÇÃO EM OPORTUNIDADE",
    "DEADLINE": "PRAZO SE APROXIMANDO",
    "WORKLOAD_KNOWN": "CARGA HORÁRIA CONFIRMADA",
    "SYSTEM": "PROBLEMA NA INFRAESTRUTURA DO SENTINELA",
}


def money(value: Decimal | float | None) -> str:
    if value is None:
        return "não informado"
    text = f"{float(value):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {text}"


def day(value: date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else "não informado"


def period(start: date | None, end: date | None) -> str:
    if start and end:
        return f"{day(start)} a {day(end)}"
    if end:
        return f"até {day(end)}"
    if start:
        return f"a partir de {day(start)}"
    return "não informado"


def idempotency_key(category: str, *parts: Any) -> str:
    """Stable across runs and processes; short enough for a unique index."""
    signature = digest([category, *[normalize(str(part)) for part in parts]])[:32]
    return f"{category.lower()}:{signature}"


def _truncate(message: str, limit: int = TELEGRAM_LIMIT) -> str:
    if len(message.encode("utf-16-le")) // 2 <= limit:
        return message
    keep = message
    while len(keep.encode("utf-16-le")) // 2 > limit - 30:
        keep = keep[: int(len(keep) * 0.9)]
    return keep.rstrip() + f"\n\n{TRUNCATION_NOTE}"


def _lines(*rows: tuple[str, str | None]) -> str:
    out: list[str] = []
    for label, value in rows:
        if value is None:
            continue
        out.append(label)
        out.append(value)
        out.append("")
    return "\n".join(out)


def opportunity_message(
    opportunity: Any, position: Any, *, category: str = "NEW_OPPORTUNITY", extra: str = ""
) -> str:
    """Plain text on purpose: untrusted edital text must not inject Telegram markup."""
    title = CATEGORY_TITLE.get(category, CATEGORY_TITLE["NEW_OPPORTUNITY"])
    qualification = position.qualification_category
    if qualification == "HIGH SCHOOL ONLY":
        qualification_text = "Nenhuma"
    else:
        extras = list(getattr(position, "additional_qualifications", []) or [])
        named = QUALIFICATION_LABEL.get(qualification, qualification)
        qualification_text = f"{named}" + (f" — {'; '.join(extras)}" if extras else "")
    workload = (
        f"{position.weekly_workload:g}h/semana"
        if position.weekly_workload
        else "não confirmada — em investigação"
    )
    assignment = position.assignment_location or "não confirmada"
    body = _lines(
        ("Instituição:", opportunity.institution),
        ("Certame:", opportunity.name),
        ("Cargo:", position.name),
        (
            "Tipo de vínculo:",
            EMPLOYMENT_LABEL.get(opportunity.employment_type, "Não confirmado")
            + (f" · {opportunity.employment_regime}" if opportunity.employment_regime else ""),
        ),
        (
            "Escolaridade:",
            EDUCATION_LABEL.get(position.education, position.education) or "não confirmada",
        ),
        ("Qualificação adicional:", qualification_text),
        ("Lotação:", f"{assignment}/AC" if position.assignment_confirmed else assignment),
        ("Carga horária:", workload),
        ("Remuneração:", money(position.salary)),
        ("Benefícios:", getattr(position, "benefits", None) or None),
        ("Vagas:", str(position.vacancies) if position.vacancies is not None else "não informado"),
        ("Cadastro de reserva:", str(position.reserve_count) if position.reserve_count else None),
        ("Inscrições:", period(opportunity.registration_start, opportunity.registration_deadline)),
        ("Taxa de inscrição:", money(opportunity.application_fee)),
        (
            "Prazo de isenção:",
            day(opportunity.fee_exemption_deadline) if opportunity.fee_exemption_deadline else None,
        ),
        ("Quem pode pedir isenção:", opportunity.fee_exemption or None),
        ("Prova:", day(opportunity.exam_date)),
        ("Banca organizadora:", opportunity.organizing_board or "não informada"),
        ("Situação:", STATUS_LABEL.get(opportunity.status, opportunity.status)),
        (
            "Classificação de carga horária:",
            WORKLOAD_LABEL.get(position.workload_classification, position.workload_classification),
        ),
        ("Compatibilidade:", f"{position.score}/100 ({position.rank})"),
        ("Confiança da informação:", position.confidence),
        ("Por que combina:", "; ".join(position.reasons or []) or "sem justificativa registrada"),
        (
            "Edital oficial:",
            opportunity.official_edital_url
            or opportunity.official_institution_url
            or "não informado",
        ),
        ("Inscrição:", opportunity.official_application_url or None),
        ("Fonte oficial:", opportunity.official_institution_url or "não informada"),
    )
    message = f"{title}\n\n{body}".rstrip()
    if extra:
        message = f"{message}\n\n{extra.strip()}"
    return _truncate(message)


def change_message(opportunity: Any, rendered_diff: str) -> str:
    header = (
        f"{CATEGORY_TITLE['CHANGE']}\n\n"
        f"Instituição:\n{opportunity.institution}\n\n"
        f"Certame:\n{opportunity.name}\n\n"
    )
    footer = (
        f"\nEdital oficial:\n"
        f"{opportunity.official_edital_url or opportunity.official_institution_url or 'não informado'}"
    )
    return _truncate(f"{header}{rendered_diff}{footer}")


def deadline_message(
    opportunity: Any,
    kind: str,
    due: date,
    days: int,
    *,
    positions: list[str] | None = None,
    extra: str = "",
) -> str:
    what = {
        "registration": "ENCERRAMENTO DAS INSCRIÇÕES",
        "exam": "DATA DA PROVA",
        "fee_exemption": "PRAZO DE ISENÇÃO DA TAXA",
        "payment": "PRAZO DE PAGAMENTO DA TAXA",
    }.get(kind, kind.upper())
    when = "é hoje" if days == 0 else f"em {days} dia{'s' if days != 1 else ''}"
    body = _lines(
        ("Instituição:", opportunity.institution),
        ("Certame:", opportunity.name),
        ("Cargos compatíveis:", "; ".join(positions or []) or None),
        ("Data:", day(due)),
        (
            "Inscrição:",
            opportunity.official_application_url or opportunity.official_institution_url or None,
        ),
        ("Edital oficial:", opportunity.official_edital_url or "não informado"),
    )
    message = f"{CATEGORY_TITLE['DEADLINE']}\n{what} {when}\n\n{body}".rstrip()
    if extra:
        message = f"{message}\n\n{extra.strip()}"
    return _truncate(message)


def system_message(title: str, detail: str, actions: list[str] | None = None) -> str:
    body = f"{CATEGORY_TITLE['SYSTEM']}\n\n{title}\n\n{detail.strip()}"
    if actions:
        body += "\n\nO que fazer:\n" + "\n".join(f"- {action}" for action in actions)
    return _truncate(body)


def deadline_alerts(due: date, today: date, thresholds: list[int]) -> int | None:
    """The single threshold to fire today, if any. Never fires two for the same date."""
    remaining = (due - today).days
    if remaining < 0:
        return None
    for threshold in sorted(thresholds, reverse=True):
        if remaining == threshold:
            return threshold
    return 0 if remaining == 0 and 0 in thresholds else None


def local_time(moment: datetime, zone: Any) -> str:
    return moment.astimezone(zone).strftime("%d/%m/%Y %H:%M")
