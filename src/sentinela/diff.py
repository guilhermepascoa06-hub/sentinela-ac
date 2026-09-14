"""Change detection and human-readable diffs.

A change is only meaningful if a person would act on it. Everything else -- a reworded
title, a new evidence URL, a re-extraction of the same value -- is stored as a version but
never becomes an alert.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from sentinela.domain import normalize

# field -> (label, is_alert_worthy)
TRACKED: dict[str, tuple[str, bool]] = {
    "registration_start": ("Início das inscrições", True),
    "registration_deadline": ("Prazo de inscrição", True),
    "exam_date": ("Data da prova", True),
    "status": ("Situação do certame", True),
    "application_fee": ("Taxa de inscrição", True),
    "fee_exemption_deadline": ("Prazo de isenção", True),
    "payment_deadline": ("Prazo de pagamento", True),
    "employment_type": ("Tipo de vínculo", True),
    "organizing_board": ("Banca organizadora", False),
    "validity": ("Validade do concurso", False),
    "selection_stages": ("Etapas da seleção", False),
    "exam_location": ("Local de prova", False),
    "official_edital_url": ("Edital oficial", False),
    "name": ("Nome do certame", False),
    "edital_number": ("Número do edital", False),
    "publication_date": ("Data de publicação", False),
}
POSITION_TRACKED: dict[str, tuple[str, bool]] = {
    "salary": ("Remuneração", True),
    "vacancies": ("Vagas", True),
    "weekly_workload": ("Carga horária semanal", True),
    "education": ("Escolaridade exigida", True),
    "qualification_category": ("Categoria de requisito", True),
    "assignment_location": ("Lotação", True),
    "assignment_confirmed": ("Lotação confirmada", True),
    "requirements": ("Requisitos", False),
    "reserve_count": ("Cadastro de reserva", False),
    "daily_workload": ("Carga horária diária", False),
    "benefits": ("Benefícios", False),
}
# Losing a known value is suspicious: it usually means a worse source overwrote a better
# one, so it is recorded but never alerted and never applied by the writer.
STATUS_ESCALATIONS = {"CANCELLED", "SUSPENDED", "REGISTRATION_OPEN", "EXAM_SCHEDULED"}


MONEY_FIELDS = frozenset({"salary", "application_fee", "benefits_value"})
DATE_FIELDS = frozenset(
    {
        "registration_start",
        "registration_deadline",
        "exam_date",
        "publication_date",
        "fee_exemption_deadline",
        "payment_deadline",
        "due_date",
    }
)
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T.*)?$")


def _present(value: Any, field: str = "") -> str:
    """Render a value the way a person reads it.

    Snapshots are stored as JSON, so a date arrives here as "2026-10-13". Formatting it
    as-is put ISO dates into the user's change alerts.
    """
    if value is None or value == "":
        return "não informado"
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, str) and (match := _ISO.match(value)):
        return f"{match[3]}/{match[2]}/{match[1]}"
    if isinstance(value, bool):
        return "sim" if value else "não"
    if isinstance(value, Decimal | float | int):
        text = f"{float(value):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        if field in MONEY_FIELDS:
            return f"R$ {text}"
        return text.rstrip("0").rstrip(",") if "," in text else text
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value) or "nenhuma"
    return str(value)


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return normalize(left) == normalize(right)
    if isinstance(left, list) and isinstance(right, list):
        return [normalize(str(x)) for x in left] == [normalize(str(y)) for y in right]
    if isinstance(left, Decimal | float | int) and isinstance(right, Decimal | float | int):
        if isinstance(left, bool) or isinstance(right, bool):
            return left == right
        return abs(float(left) - float(right)) < 0.005
    return left == right


def compare(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    scope: str = "opportunity",
    label: str = "",
) -> list[dict[str, Any]]:
    """Field-level changes between two snapshots, each carrying its own audit record."""
    tracked = TRACKED if scope == "opportunity" else POSITION_TRACKED
    changes: list[dict[str, Any]] = []
    for field, (title, alertable) in tracked.items():
        before, after = previous.get(field), current.get(field)
        if _equal(before, after):
            continue
        lost = after in (None, "", []) and before not in (None, "", [])
        changes.append(
            {
                "scope": scope,
                "subject": label,
                "field": field,
                "label": title,
                "old_value": _present(before, field),
                "new_value": _present(after, field),
                "raw_old": before
                if isinstance(before, str | int | float | bool | type(None))
                else str(before),
                "raw_new": after
                if isinstance(after, str | int | float | bool | type(None))
                else str(after),
                # A value that disappeared is an extraction regression, not news for the user.
                "alertable": bool(alertable and not lost),
                "information_lost": lost,
            }
        )
    return changes


def compare_positions(
    previous: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    before = {str(item.get("slug")): item for item in previous}
    after = {str(item.get("slug")): item for item in current}
    changes: list[dict[str, Any]] = []
    for slug, item in after.items():
        name = str(item.get("name") or slug)
        if slug not in before:
            changes.append(
                {
                    "scope": "position",
                    "subject": name,
                    "field": "position",
                    "label": "Novo cargo",
                    "old_value": "não existia",
                    "new_value": name,
                    "raw_old": None,
                    "raw_new": name,
                    "alertable": True,
                    "information_lost": False,
                }
            )
            continue
        changes.extend(compare(before[slug], item, scope="position", label=name))
    for slug, item in before.items():
        if slug not in after:
            changes.append(
                {
                    "scope": "position",
                    "subject": str(item.get("name") or slug),
                    "field": "position",
                    "label": "Cargo ausente nesta versão",
                    "old_value": str(item.get("name") or slug),
                    "new_value": "não consta",
                    "raw_old": str(item.get("name")),
                    "raw_new": None,
                    "alertable": False,
                    "information_lost": True,
                }
            )
    return changes


def alertable(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [change for change in changes if change.get("alertable")]


def render(changes: list[dict[str, Any]], *, source: str = "", document: str = "") -> str:
    """The human-readable diff that goes into the notification."""
    if not changes:
        return ""
    lines: list[str] = []
    for change in changes:
        heading = change["label"].upper()
        if change.get("subject"):
            heading = f"{heading} — {change['subject']}"
        lines.append(heading)
        lines.append("")
        lines.append("Antes:")
        lines.append(str(change["old_value"]))
        lines.append("")
        lines.append("Agora:")
        lines.append(str(change["new_value"]))
        lines.append("")
    if document:
        lines.append(f"Documento: {document}")
    if source:
        lines.append(f"Fonte: {source}")
    return "\n".join(lines).strip()


def status_escalated(previous: str | None, current: str | None) -> bool:
    return bool(current and current != previous and current in STATUS_ESCALATIONS)
