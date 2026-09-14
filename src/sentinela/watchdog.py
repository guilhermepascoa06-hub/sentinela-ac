"""The monitor's monitor.

The failure this guards against is the worst one available: the user believing the system
is watching for concursos while it has silently stopped. Silence is therefore never
treated as good news -- the absence of a recent heartbeat is itself an alert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from sentinela import alerts
from sentinela.config import Configuration, Secrets
from sentinela.domain import digest, now, utc
from sentinela.models import MonitorRun, Notification, SourceHealth


@dataclass(slots=True)
class WatchdogVerdict:
    healthy: bool
    severity: str  # OK | WARNING | CRITICAL
    title: str
    detail: str
    actions: list[str]
    last_success: datetime | None
    hours_since: float | None
    # Stable signature of WHAT is wrong, so an unchanged situation is not re-announced.
    fingerprint: str = ""


def evaluate(
    session: Session, config: Configuration, moment: datetime | None = None
) -> WatchdogVerdict:
    reference = moment or now()
    last = session.execute(
        sa.select(MonitorRun)
        .where(MonitorRun.status.in_(["SUCCESS", "PARTIAL"]))
        .order_by(MonitorRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    limit = config.monitoring.watchdog_hours

    if last is None:
        return WatchdogVerdict(
            False,
            "CRITICAL",
            "Nenhuma execução bem-sucedida registrada",
            "O Sentinela nunca concluiu uma execução de monitoramento. Nada está sendo "
            "vigiado neste momento.",
            [
                "Rodar `sentinela doctor` para checar banco e credenciais",
                "Conferir se o workflow daily-monitor está habilitado no GitHub Actions",
            ],
            None,
            None,
        )
    elapsed = (reference - utc(last.started_at)).total_seconds() / 3600
    if elapsed > limit:
        return WatchdogVerdict(
            False,
            "CRITICAL",
            f"Sem monitoramento há {elapsed:.0f}h (limite {limit}h)",
            f"A última execução bem-sucedida começou em {utc(last.started_at).astimezone(config.zone):%d/%m/%Y %H:%M} "
            f"(horário de {config.profile.timezone}). O monitoramento parou de rodar.",
            [
                "Verificar as execuções recentes em GitHub Actions",
                "Confirmar que o segredo DATABASE_URL continua válido",
                "Rodar `sentinela doctor`",
            ],
            utc(last.started_at),
            elapsed,
        )

    stale_sources = session.execute(
        sa.select(sa.func.count())
        .select_from(SourceHealth)
        .where(SourceHealth.state.in_(["OPEN", "DISABLED"]))
    ).scalar_one()
    stuck = session.execute(
        sa.select(sa.func.count())
        .select_from(Notification)
        .where(Notification.status.in_(["RETRY", "UNCERTAIN", "FAILED"]))
    ).scalar_one()
    running_too_long = session.execute(
        sa.select(sa.func.count())
        .select_from(MonitorRun)
        .where(
            MonitorRun.status == "RUNNING",
            MonitorRun.started_at
            < reference - timedelta(minutes=config.monitoring.max_run_minutes * 2),
        )
    ).scalar_one()

    problems: list[str] = []
    if stale_sources:
        problems.append(f"{stale_sources} fonte(s) com circuito aberto ou desativadas")
    if stuck:
        problems.append(f"{stuck} notificação(ões) pendentes de entrega ou revisão")
    if running_too_long:
        problems.append(f"{running_too_long} execução(ões) presa(s) no estado RUNNING")
    if problems:
        return WatchdogVerdict(
            True,
            "WARNING",
            "Monitoramento ativo, com pendências",
            "Última execução dentro do prazo, mas há itens a resolver:\n- " + "\n- ".join(problems),
            [
                "Rodar `sentinela sources` para ver o estado das fontes",
                "Rodar `sentinela doctor` para o diagnóstico completo",
            ],
            utc(last.started_at),
            elapsed,
            fingerprint=digest(sorted(problems)),
        )
    return WatchdogVerdict(
        True,
        "OK",
        "Monitoramento ativo",
        f"Última execução há {elapsed:.1f}h.",
        [],
        utc(last.started_at),
        elapsed,
    )


def notify(
    session: Session, verdict: WatchdogVerdict, config: Configuration, secrets: Secrets
) -> int:
    """How often the user hears about a problem depends on what kind of problem it is.

    CRITICAL means nothing is being monitored right now: repeat it every day until it is
    fixed. WARNING means the system works but something needs attention -- announce it
    when the situation CHANGES and then stay quiet. Repeating "9 sources are degraded"
    every morning about the same nine permanently-broken portals is exactly the useless
    daily message this project refuses to send; the per-source GitHub issues already
    carry the detail and survive until someone closes them.
    """
    from sentinela.pipeline import channels_for, queue_notification

    if verdict.severity == "OK":
        return 0
    channels = channels_for(config)
    if not channels:
        return 0
    scope = (
        now().astimezone(config.zone).date().isoformat()
        if verdict.severity == "CRITICAL"
        else verdict.fingerprint
    )
    return queue_notification(
        session,
        key=alerts.idempotency_key("SYSTEM", "watchdog", verdict.title, scope),
        category="SYSTEM",
        body=alerts.system_message(verdict.title, verdict.detail, verdict.actions),
        channels=channels,
        subject="Sentinela AC — alerta de infraestrutura",
    )


def heartbeat_payload(verdict: WatchdogVerdict) -> dict[str, Any]:
    return {
        "healthy": verdict.healthy,
        "severity": verdict.severity,
        "title": verdict.title,
        "last_success": verdict.last_success.isoformat() if verdict.last_success else None,
        "hours_since": round(verdict.hours_since, 2) if verdict.hours_since is not None else None,
    }


def ping(secrets: Secrets, payload: dict[str, Any]) -> str:
    """Optional external dead-man switch (healthchecks.io and similar)."""
    url = secrets.watchdog_ping_url.get_secret_value()
    if not url:
        return "SKIPPED"
    import httpx

    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(url, json=payload)
        return "OK" if response.status_code < 400 else f"HTTP {response.status_code}"
    except httpx.HTTPError as error:
        return type(error).__name__
