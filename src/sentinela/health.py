"""Source health, circuit breaker and schema-drift detection.

The core invariant: a source returning zero documents is never taken as evidence that
there are zero concursos. It is taken as evidence that the source needs looking at.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sentinela.domain import digest

HEALTHY, DEGRADED, OPEN, RECOVERING, DISABLED = (
    "HEALTHY",
    "DEGRADED",
    "OPEN",
    "RECOVERING",
    "DISABLED",
)
HISTORY_LENGTH = 20


@dataclass(slots=True)
class DriftVerdict:
    detected: bool
    reason: str = ""
    evidence: dict[str, Any] | None = None


def fingerprint(structure: dict[str, Any]) -> str:
    """Shape-only fingerprint: content changes daily, structure should not."""
    shape = {
        key: structure.get(key)
        for key in ("links", "tables", "lists", "headings", "pages")
        if structure.get(key) is not None
    }
    return digest(shape)


def detect_drift(
    *,
    documents_found: int,
    history: list[int],
    previous_fingerprint: str | None,
    current_fingerprint: str | None,
    expected_min: int,
    http_status: int | None,
) -> DriftVerdict:
    """Decide whether an empty or thin result means 'nothing new' or 'the page changed'."""
    if http_status is not None and http_status >= 400:
        return DriftVerdict(False)  # an HTTP failure is a fetch problem, not drift
    structure_changed = bool(
        previous_fingerprint and current_fingerprint and previous_fingerprint != current_fingerprint
    )
    if documents_found == 0 and expected_min > 0:
        return DriftVerdict(
            True,
            f"Fonte devolveu 0 documentos com mínimo esperado de {expected_min}",
            {"documents": 0, "expected_min": expected_min},
        )
    usable_history = [value for value in history if value > 0]
    if documents_found == 0 and usable_history:
        return DriftVerdict(
            True,
            f"Fonte devolveu 0 documentos após histórico de {min(usable_history)}–"
            f"{max(usable_history)}",
            {"documents": 0, "history": usable_history[-HISTORY_LENGTH:]},
        )
    if usable_history and len(usable_history) >= 3:
        median = statistics.median(usable_history)
        if median >= 4 and documents_found < median * 0.25:
            return DriftVerdict(
                True,
                f"Queda abrupta de documentos: {documents_found} contra mediana {median:.0f}",
                {"documents": documents_found, "median": median},
            )
    if structure_changed and documents_found == 0:
        return DriftVerdict(
            True,
            "Estrutura do HTML mudou e nenhum documento foi reconhecido",
            {"previous": previous_fingerprint, "current": current_fingerprint},
        )
    return DriftVerdict(False)


@dataclass(slots=True)
class HealthTransition:
    state: str
    open_until: datetime | None
    should_alert: bool
    reason: str


def next_state(
    *,
    current: str,
    success: bool,
    consecutive_failures: int,
    drift: bool,
    failure_threshold: int,
    cooldown_hours: int,
    now: datetime | None = None,
    open_until: datetime | None = None,
) -> HealthTransition:
    """HEALTHY -> DEGRADED -> OPEN -> (cooldown) -> RECOVERING -> HEALTHY."""
    moment = now or datetime.now(UTC)
    if current == DISABLED:
        return HealthTransition(DISABLED, None, False, "Fonte desativada manualmente")
    if success and not drift:
        if current in (OPEN, RECOVERING, DEGRADED):
            return HealthTransition(HEALTHY, None, False, "Fonte recuperada")
        return HealthTransition(HEALTHY, None, False, "Coleta normal")
    if success and drift:
        return HealthTransition(
            DEGRADED,
            open_until,
            current != DEGRADED,
            "Resposta válida mas sem os documentos esperados: possível mudança de layout",
        )
    if consecutive_failures >= failure_threshold:
        return HealthTransition(
            OPEN,
            moment + timedelta(hours=cooldown_hours),
            current != OPEN,
            f"{consecutive_failures} falhas consecutivas: circuito aberto por {cooldown_hours}h",
        )
    return HealthTransition(
        DEGRADED, open_until, False, f"Falha {consecutive_failures} de {failure_threshold}"
    )


def should_skip(
    state: str, open_until: datetime | None, now: datetime | None = None
) -> tuple[bool, str]:
    """Never hammer a broken government website; retry once the cooling period ends."""
    moment = now or datetime.now(UTC)
    if state == DISABLED:
        return True, "Fonte desativada"
    if state == OPEN:
        if open_until and moment < open_until:
            return True, f"Circuito aberto até {open_until:%d/%m %H:%M} UTC"
        return False, "Fim do período de espera: testando novamente"
    return False, ""


def entering_recovery(state: str, open_until: datetime | None, now: datetime | None = None) -> bool:
    moment = now or datetime.now(UTC)
    return state == OPEN and open_until is not None and moment >= open_until


def push_history(history: list[Any], value: int) -> list[str]:
    """History is stored as strings so the JSON column stays uniform across dialects."""
    entries = [str(item) for item in (history or [])][-(HISTORY_LENGTH - 1) :]
    entries.append(str(value))
    return entries


def read_history(history: list[Any]) -> list[int]:
    values: list[int] = []
    for item in history or []:
        try:
            values.append(int(item))
        except (TypeError, ValueError):
            continue
    return values


def parser_rate(successes: int, attempts: int) -> float:
    return round(successes / attempts, 3) if attempts else 0.0


def summarize(states: list[str]) -> dict[str, int]:
    return {
        "healthy": states.count(HEALTHY),
        "degraded": states.count(DEGRADED),
        "open": states.count(OPEN),
        "recovering": states.count(RECOVERING),
        "disabled": states.count(DISABLED),
    }
