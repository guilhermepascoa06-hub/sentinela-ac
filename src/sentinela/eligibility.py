from datetime import date
from typing import Protocol

from sentinela.alerts import WORKLOAD_LABEL
from sentinela.config import Configuration
from sentinela.domain import Eligibility, OpportunityDraft, PositionDraft, normalize


class Scheduled(Protocol):
    """Anything with a certame schedule: the parsed draft or the stored opportunity."""

    status: str
    registration_start: date | None
    registration_deadline: date | None
    exam_date: date | None


def effective_status(item: Scheduled, today: date) -> str:
    if item.status in {"CANCELLED", "SUSPENDED", "HOMOLOGATED", "EXPIRED"}:
        return item.status
    if item.registration_deadline and today > item.registration_deadline:
        if item.exam_date and today <= item.exam_date:
            return "EXAM_SCHEDULED"
        return "REGISTRATION_CLOSED"
    if item.registration_start and item.registration_deadline:
        if item.registration_start <= today <= item.registration_deadline:
            return "REGISTRATION_OPEN"
    return item.status


def classify_qualification(requirements: str | None) -> tuple[str, str | None, list[str]]:
    text = normalize(requirements or "")
    if not text:
        return "UNKNOWN", None, []
    if any(word in text for word in ["nivel superior", "graduacao", "licenciatura", "bacharel"]):
        return "OTHER ADDITIONAL REQUIREMENT", "higher_education", [requirements or ""]
    if not any(word in text for word in ["ensino medio", "nivel medio", "2 grau"]):
        return "UNKNOWN", "primary" if "fundamental" in text else None, []
    extras: list[str] = []
    category = "HIGH SCHOOL ONLY"
    if any(word in text for word in ["tecnico em", "curso tecnico", "formacao tecnica"]):
        category = "HIGH SCHOOL + TECHNICAL QUALIFICATION"
        extras.append("Curso técnico")
    if any(word in text for word in ["registro", "conselho", "corem", "coren"]):
        # A diploma 'devidamente registrado' is NOT professional registration.
        if any(word in text for word in ["conselho", "orgao de classe", "registro profissional"]):
            category = "HIGH SCHOOL + PROFESSIONAL REGISTRATION"
            extras.append("Registro profissional")
    if any(word in text for word in ["cnh", "habilitacao categoria", "carteira nacional"]):
        category = "HIGH SCHOOL + DRIVER/LICENSE REQUIREMENT"
        extras.append("CNH/licença")
    if any(
        word in text
        for word in [
            "prolibras",
            "proficiencia",
            "experiencia",
            "curso de formacao",
            "educacao profissional",
            "residir",
            "idade maxima",
        ]
    ):
        if not extras:
            category = "OTHER ADDITIONAL REQUIREMENT"
        extras.append("Qualificação/condição adicional: consultar requisito integral")
    return category, "high_school", extras


def evaluate(
    item: OpportunityDraft,
    position: PositionDraft,
    config: Configuration,
    today: date,
    trust_level: int = 1,
) -> Eligibility:
    hours = position.weekly_workload
    wc = (
        "UNKNOWN"
        if hours is None
        else (
            "PERFECT"
            if hours <= config.workload.ideal_max_weekly_hours
            else "GOOD"
            if hours <= config.workload.acceptable_max_weekly_hours
            else "OUTSIDE TARGET"
        )
    )
    locations = [position.assignment_location or "", *position.possible_assignment_locations]
    local = position.assignment_confirmed and any(
        normalize(config.profile.city) in normalize(place) for place in locations
    )
    school = position.education in config.education["accepted"]
    plain = (
        position.qualification_category == "HIGH SCHOOL ONLY"
        and not position.additional_qualifications
    )
    state = effective_status(item, today)
    evidence_fields = [
        position.evidence.get(k)
        for k in ("education", "requirements", "assignment_location", "weekly_workload")
    ]
    conflict = bool(item.conflicts) or any(
        e and e.status in {"CONFLICTING", "AMBIGUOUS"} for e in evidence_fields
    )
    published = item.publication_date
    dated = bool(published and published <= today)
    fresh = bool(
        published and dated and (today - published).days <= config.monitoring.freshness_days
    )
    open_now = state == "REGISTRATION_OPEN"
    actionable = open_now or (
        state == "EDITAL_PUBLISHED"
        and fresh
        and (not item.registration_deadline or item.registration_deadline >= today)
    )
    score = (
        30 * local
        + 25 * (school and plain)
        + (25 if wc == "PERFECT" else 15 if wc == "GOOD" else 5 if wc == "UNKNOWN" else -25)
        + (
            10
            if item.employment_type == "PERMANENT"
            else -10
            if item.employment_type == "TEMPORARY"
            else 0
        )
    )
    score += 7 * open_now + 3 * bool(item.exam_date)
    score -= 20 * (not local) + 15 * (not plain)
    score = max(0, min(100, score))
    reasons = []
    reasons.append(
        "Lotação confirmada em Rio Branco" if local else "Lotação em Rio Branco não confirmada"
    )
    reasons.append(
        "Ensino médio sem qualificação adicional"
        if school and plain
        else "Escolaridade/requisitos fora do filtro principal ou não confirmados"
    )
    # Stored reasons are read by a person in the panel, the bot and the alert: the
    # classification code is not an answer to "why does this fit me?".
    reasons.append(f"Carga horária: {WORKLOAD_LABEL.get(wc, wc)}")
    if conflict:
        reasons.append("Conflito de evidências: revisão necessária")
    if not actionable:
        reasons.append("Sem inscrições abertas ou edital recente acionável")
    if not dated:
        reasons.append("Data de publicação não confirmada")
    eligible = local and school and plain and wc != "OUTSIDE TARGET"
    certain = [e.confidence for e in evidence_fields if e and e.status == "FOUND"]
    confidence = (
        "HIGH"
        if trust_level <= 2 and len(certain) >= 3 and min(certain) >= 0.9 and not conflict
        else "MEDIUM"
        if trust_level <= 2 and certain and not conflict
        else "LOW"
    )
    return Eligibility(
        eligible=eligible,
        primary_alert=bool(eligible and actionable and dated and trust_level <= 2 and not conflict),
        workload_classification=wc,
        score=score,
        rank="EXCELLENT"
        if score >= 90
        else "VERY GOOD"
        if score >= 75
        else "REVIEW"
        if score >= 60
        else "LOW PRIORITY",
        confidence=confidence,
        reasons=reasons,
        needs_review=bool(not local or not school or hours is None or conflict or not dated),
    )
