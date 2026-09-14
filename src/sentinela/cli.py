"""Command line interface. Every command is safe to run twice."""

from __future__ import annotations

import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import sqlalchemy as sa
import typer
from rich.console import Console
from rich.table import Table

from sentinela import audit as audit_module
from sentinela import backup as backup_module
from sentinela import report as report_module
from sentinela import watchdog as watchdog_module
from sentinela.config import Configuration, Secrets, load_config
from sentinela.db import (
    DatabaseUnavailable,
    advisory_lock,
    create_engine,
    database_size_mb,
    migration_state,
    ping,
    resolve_url,
    session_factory,
    session_scope,
)
from sentinela.domain import now, utc
from sentinela.logging import RunLogger, configure, get
from sentinela.models import (
    Deadline,
    Document,
    MonitorRun,
    Notification,
    Opportunity,
    Position,
    Source,
    SourceHealth,
)
from sentinela.registry import load_registry, sync

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Sentinela AC — monitor auditável de concursos em Rio Branco/AC.",
)
console = Console()


def _context(database_url: str | None = None) -> tuple[Configuration, Secrets, Any]:
    config = load_config()
    secrets = Secrets()
    engine = create_engine(resolve_url(secrets, database_url))
    return config, secrets, engine


def _fail(message: str) -> None:
    console.print(f"[bold red]{message}[/bold red]")
    raise typer.Exit(code=1)


@app.command()
def run(
    only: Annotated[list[str] | None, typer.Option(help="Coletar apenas estas fontes.")] = None,
    kind: Annotated[str, typer.Option(help="daily | backup | manual")] = "daily",
    trigger: Annotated[str, typer.Option(help="Origem da execução.")] = "manual",
    dry_run: Annotated[bool, typer.Option(help="Coleta sem gravar nem notificar.")] = False,
    force: Annotated[
        bool,
        typer.Option(help="Coletar mesmo com o circuito aberto (para testar um conserto)."),
    ] = False,
    skip_if_fresh: Annotated[
        bool,
        typer.Option(
            help="Sair cedo se a execução primária de hoje já concluiu (uso do run de backup)."
        ),
    ] = False,
    log_level: Annotated[str, typer.Option()] = "INFO",
) -> None:
    """Executa um ciclo completo de monitoramento."""
    from sentinela.pipeline import finalize_run, run_is_fresh, run_monitor

    configure(log_level)
    logger = RunLogger(get("cli"), {})
    try:
        config, secrets, engine = _context()
    except DatabaseUnavailable as error:
        _fail(str(error))
        return
    ok, detail = ping(engine)
    if not ok:
        _fail(f"Banco indisponível: {detail}")
    applied, revision = migration_state(engine)
    if not applied:
        _fail(f"Migrations pendentes ({revision}). Rode: alembic upgrade head")

    factory = session_factory(engine)
    today = now().astimezone(config.zone).date()
    if skip_if_fresh:
        with session_scope(factory) as session:
            fresh, status = run_is_fresh(session, config, today)
        if fresh:
            console.print(
                f"[green]Execução primária de hoje já concluída ({status}). Saindo.[/green]"
            )
            return
        console.print(
            f"[yellow]Execução primária ausente ou incompleta ({status}). Assumindo o ciclo.[/yellow]"
        )

    # One full cycle at a time, enforced by the database rather than by the scheduler.
    with advisory_lock(engine, "monitor") as acquired:
        if not acquired:
            console.print("[yellow]Outra execução está em andamento. Nada a fazer.[/yellow]")
            return
        with session_scope(factory) as session:
            sync(session, load_registry())
        with session_scope(factory) as session:
            result = run_monitor(
                session,
                config,
                secrets,
                kind=kind,
                trigger=trigger,
                only=only or None,
                force=force,
                dry_run=dry_run,
                logger=logger,
            )
        with session_scope(factory) as session:
            finalize_run(session, config, secrets, result, logger)
            path = report_module.write(session, config)

    table = Table(title=f"Execução {result.status}", show_header=False)
    table.add_row(
        "Fontes",
        f"{result.sources_successful}/{result.sources_attempted} "
        f"(falhas {result.sources_failed}, ignoradas {result.sources_skipped})",
    )
    table.add_row(
        "Documentos",
        f"{result.documents_discovered} vistos · "
        f"{result.documents_new} novos · {result.documents_changed} alterados",
    )
    table.add_row(
        "Oportunidades",
        f"{result.opportunities_created} novas · {result.opportunities_updated} atualizadas",
    )
    table.add_row(
        "Notificações",
        f"{result.notifications_queued} na fila · {result.notifications_sent} entregues",
    )
    table.add_row("Fila de revisão", str(result.review_queue))
    table.add_row("Fontes degradadas", ", ".join(sorted(set(result.degraded))) or "nenhuma")
    table.add_row("Erros", str(result.errors))
    table.add_row("Relatório", str(path))
    console.print(table)
    if result.status == "FAILED":
        raise typer.Exit(code=1)


@app.command()
def report(
    write: Annotated[bool, typer.Option(help="Gravar reports/latest.md.")] = True,
    show: Annotated[bool, typer.Option(help="Imprimir no terminal.")] = False,
) -> None:
    """Gera o relatório completo em reports/latest.md."""
    config, _, engine = _context()
    with session_scope(session_factory(engine)) as session:
        content = report_module.build(session, config)
        path = report_module.write(session, config) if write else None
    if show:
        console.print(content)
    if path:
        console.print(f"[green]Relatório gravado em {path}[/green]")


@app.command()
def opportunities(
    limit: Annotated[int, typer.Option()] = 25,
    all_records: Annotated[bool, typer.Option("--all", help="Incluir não compatíveis.")] = False,
    unknown_workload: Annotated[bool, typer.Option(help="Só carga horária desconhecida.")] = False,
) -> None:
    """Lista as oportunidades armazenadas."""
    config, _, engine = _context()
    with session_scope(session_factory(engine)) as session:
        query = (
            sa.select(Opportunity, Position)
            .join(Position, Position.opportunity_id == Opportunity.id)
            .order_by(Position.score.desc(), Opportunity.registration_deadline)
        )
        if not all_records:
            query = query.where(Position.eligible.is_(True))
        if unknown_workload:
            query = query.where(Position.weekly_workload.is_(None))
        rows = list(session.execute(query.limit(limit)).all())
        table = Table(title="Oportunidades")
        for column in (
            "Cargo",
            "Instituição",
            "CH",
            "Remuneração",
            "Inscrições até",
            "Score",
            "Confiança",
            "Situação",
        ):
            table.add_column(column, overflow="fold")
        for opportunity, position in rows:
            table.add_row(
                position.name[:40],
                opportunity.institution[:28],
                f"{position.weekly_workload:g}h" if position.weekly_workload else "?",
                f"{position.salary:,.2f}" if position.salary else "?",
                opportunity.registration_deadline.strftime("%d/%m/%Y")
                if opportunity.registration_deadline
                else "?",
                f"{position.score}",
                position.confidence,
                opportunity.status,
            )
    console.print(table)
    console.print(f"[dim]{len(rows)} registro(s) · fuso {config.profile.timezone}[/dim]")


@app.command()
def deadlines(days: Annotated[int, typer.Option()] = 45) -> None:
    """Prazos ativos nos próximos dias."""
    config, _, engine = _context()
    today = now().astimezone(config.zone).date()
    with session_scope(session_factory(engine)) as session:
        rows = session.execute(
            sa.select(Deadline, Opportunity)
            .join(Opportunity, Opportunity.id == Deadline.opportunity_id)
            .where(
                Deadline.active.is_(True),
                Deadline.due_date >= today,
                Deadline.due_date <= today + timedelta(days=days),
            )
            .order_by(Deadline.due_date)
        ).all()
        table = Table(title=f"Prazos até {days} dias")
        for column in ("Data", "Faltam", "Tipo", "Instituição", "Certame", "Compatível"):
            table.add_column(column, overflow="fold")
        for deadline, opportunity in rows:
            table.add_row(
                deadline.due_date.strftime("%d/%m/%Y"),
                f"{(deadline.due_date - today).days}d",
                deadline.description[:26],
                opportunity.institution[:26],
                opportunity.name[:40],
                "sim" if opportunity.eligible else "não",
            )
    console.print(table)


@app.command()
def sources(
    degraded: Annotated[bool, typer.Option(help="Só fontes com problema.")] = False,
) -> None:
    """Estado de saúde de cada fonte monitorada."""
    config, _, engine = _context()
    with session_scope(session_factory(engine)) as session:
        sync(session, load_registry())
        query = (
            sa.select(Source, SourceHealth)
            .join(SourceHealth, SourceHealth.source_id == Source.id)
            .order_by(Source.priority, Source.id)
        )
        if degraded:
            query = query.where(SourceHealth.state.notin_(["HEALTHY"]))
        rows = list(session.execute(query).all())
        table = Table(title="Fontes")
        for column in ("ID", "Nível", "Estado", "Falhas", "Última OK", "Docs", "Parser"):
            table.add_column(column, overflow="fold")
        for source, state in rows:
            from sentinela.health import parser_rate

            table.add_row(
                source.id,
                f"L{source.trust_level}{'*' if source.trust_status == 'CANDIDATE' else ''}",
                state.state,
                str(state.consecutive_failures),
                utc(state.last_success).astimezone(config.zone).strftime("%d/%m %H:%M")
                if state.last_success
                else "nunca",
                str(state.documents_last_run or 0),
                f"{parser_rate(state.parser_successes, state.parser_attempts):.0%}",
            )
    console.print(table)


@app.command("source-check")
def source_check(source_id: Annotated[str, typer.Argument()]) -> None:
    """Coleta uma única fonte agora, sem tocar no circuito das demais."""
    from sentinela.pipeline import run_monitor

    configure("INFO")
    config, secrets, engine = _context()
    factory = session_factory(engine)
    with session_scope(factory) as session:
        sync(session, load_registry())
        if session.get(Source, source_id) is None:
            _fail(f"Fonte desconhecida: {source_id}")
    with session_scope(factory) as session:
        result = run_monitor(
            session, config, secrets, kind="manual", trigger="source-check", only=[source_id]
        )
    console.print(
        f"[green]{source_id}: {result.documents_discovered} documento(s), "
        f"{result.opportunities_created} nova(s), status {result.status}[/green]"
    )


@app.command()
def doctor() -> None:
    """Diagnóstico completo de banco, migrations, fontes, notificações e watchdog."""
    from sentinela.pipeline import utc_schedule

    config = load_config()
    secrets = Secrets()
    console.print("[bold]Sentinela AC Doctor[/bold]\n")
    checks: list[tuple[str, str, str]] = []

    try:
        engine = create_engine(resolve_url(secrets))
        ok, detail = ping(engine)
        checks.append(("Banco de dados", "OK" if ok else "FALHOU", detail))
    except DatabaseUnavailable as error:
        console.print(f"[red]Banco de dados: NÃO CONFIGURADO[/red] — {error}")
        raise typer.Exit(code=1) from error
    if not ok:
        console.print(f"[red]Banco de dados: FALHOU[/red] — {detail}")
        raise typer.Exit(code=1)

    applied, revision = migration_state(engine)
    checks.append(("Migrations", "OK" if applied else "PENDENTE", revision))

    from sentinela.notifications import telegram_health

    telegram = telegram_health(secrets)
    checks.append(("Telegram", telegram["status"], telegram["detail"]))
    checks.append(
        (
            "E-mail",
            "OK" if secrets.smtp_host else "DESATIVADO",
            secrets.email_to or "sem destinatário",
        )
    )
    llm_on = bool(config.llm.get("enabled"))
    checks.append(
        (
            "LLM",
            "ATIVO" if llm_on else "DESATIVADO",
            str(config.llm.get("model") or "extração 100% determinística"),
        )
    )
    checks.append(
        (
            "GitHub (issues)",
            "OK" if secrets.github_repository else "DESATIVADO",
            secrets.github_repository or "GITHUB_REPOSITORY ausente",
        )
    )

    try:
        specs = load_registry()
        checks.append(("Registry", "OK", f"{len(specs)} fontes declaradas"))
    except (ValueError, OSError) as error:
        checks.append(("Registry", "FALHOU", str(error)))
        specs = []

    with session_scope(session_factory(engine)) as session:
        if specs:
            sync(session, specs)
        states = list(session.execute(sa.select(SourceHealth.state)).scalars().all())
        from sentinela.health import summarize

        summary = summarize(states)
        last = session.execute(
            sa.select(MonitorRun)
            .where(MonitorRun.status.in_(["SUCCESS", "PARTIAL"]))
            .order_by(MonitorRun.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        verdict = watchdog_module.evaluate(session, config)
        pending = session.execute(
            sa.select(Notification.status, sa.func.count()).group_by(Notification.status)
        ).all()
        review = session.execute(
            sa.select(sa.func.count())
            .select_from(Document)
            .where(Document.processing_state == "REVIEW")
        ).scalar_one()

    table = Table(show_header=False)
    for name, status, detail in checks:
        colour = {"OK": "green", "ATIVO": "green"}.get(
            status, "yellow" if status in ("DESATIVADO", "PENDENTE", "DEGRADED") else "red"
        )
        table.add_row(name, f"[{colour}]{status}[/{colour}]", detail[:70])
    table.add_row(
        "Fontes",
        f"{summary['healthy']} saudáveis",
        f"{summary['degraded']} degradadas · {summary['open']} circuito aberto · "
        f"{summary['recovering']} recuperando · {summary['disabled']} desativadas",
    )
    table.add_row(
        "Última execução OK",
        utc(last.started_at).astimezone(config.zone).strftime("%d/%m/%Y %H:%M")
        if last
        else "nenhuma",
        config.profile.timezone,
    )
    table.add_row("Watchdog", verdict.severity, verdict.title)
    table.add_row("Notificações", ", ".join(f"{s} {c}" for s, c in pending) or "vazio", "")
    table.add_row("Fila de revisão", str(review), "documentos aguardando reprocessamento")
    used = database_size_mb(engine)
    aviso = float(config.storage.get("warning_size_mb") or 0)
    if used is not None:
        colour = "red" if aviso and used >= aviso else "green"
        table.add_row(
            "Armazenamento",
            f"[{colour}]{used:.0f} MB[/{colour}]",
            f"aviso em {aviso:.0f} MB" if aviso else "sem limite configurado",
        )
    table.add_row(
        "Agenda (UTC)",
        utc_schedule(config.monitoring.primary_local_time, config.profile.timezone),
        f"backup {utc_schedule(config.monitoring.backup_local_time, config.profile.timezone)}",
    )
    console.print(table)
    if verdict.severity == "CRITICAL" or not applied:
        raise typer.Exit(code=1)


@app.command("deep-audit")
def deep_audit(
    open_issues: Annotated[
        bool, typer.Option(help="Abrir issues no GitHub para fontes degradadas.")
    ] = True,
) -> None:
    """Auditoria semanal: revalida fontes, reprocessa pendências e descobre novas fontes."""
    configure("INFO")
    config, secrets, engine = _context()
    factory = session_factory(engine)
    with advisory_lock(engine, "deep-audit") as acquired:
        if not acquired:
            console.print("[yellow]Auditoria já em andamento.[/yellow]")
            return
        with session_scope(factory) as session:
            sync(session, load_registry())
            result = audit_module.run_deep_audit(session, config, secrets)
            issues = (
                audit_module.report_degraded_sources(session, config, secrets)
                if open_issues
                else []
            )
            report_module.write(session, config)
    table = Table(title="Auditoria semanal", show_header=False)
    table.add_row("Fontes revalidadas", str(result.sources_revalidated))
    table.add_row("Fontes com problema", ", ".join(result.sources_broken) or "nenhuma")
    table.add_row("URLs alteradas", str(len(result.urls_changed)))
    table.add_row(
        "Documentos reprocessados",
        f"{result.documents_retried} (recuperados {result.documents_recovered})",
    )
    table.add_row("Cargas horárias ainda desconhecidas", str(result.workloads_resolved))
    table.add_row("Certames expirados", str(result.statuses_expired))
    table.add_row(
        "Fontes candidatas descobertas", ", ".join(result.candidates_discovered) or "nenhuma"
    )
    table.add_row("Links de edital quebrados", str(len(result.links_broken)))
    table.add_row("Conflitos em aberto", str(result.conflicts_open))
    table.add_row("Issues abertas", ", ".join(f"#{number}" for number in issues) or "nenhuma")
    console.print(table)


@app.command()
def issues() -> None:
    """Abre issue no GitHub para cada fonte degradada sem issue aberta."""
    config, secrets, engine = _context()
    with session_scope(session_factory(engine)) as session:
        sync(session, load_registry())
        created = audit_module.report_degraded_sources(session, config, secrets)
    if not (secrets.github_repository and secrets.github_token.get_secret_value()):
        # Say so plainly: "0 issues" would read as "nada quebrado", which is not the same
        # thing as "não consegui nem tentar".
        console.print(
            "[yellow]Sem GITHUB_TOKEN/GITHUB_REPOSITORY: nenhuma issue foi verificada "
            "nem aberta. Dentro do GitHub Actions os dois existem automaticamente.[/yellow]"
        )
        return
    console.print(
        f"[green]{len(created)} issue(s) aberta(s): "
        f"{', '.join(f'#{number}' for number in created) or 'nenhuma nova'}[/green]"
    )


@app.command()
def retry(
    limit: Annotated[int, typer.Option(help="Máximo de documentos a reprocessar.")] = 40,
) -> None:
    """Reprocessa documentos parados na fila de revisão e reenvia notificações pendentes."""
    configure("INFO")
    config, secrets, engine = _context()
    logger = RunLogger(get("retry"), {"stage": "retry"})
    with session_scope(session_factory(engine)) as session:
        result = audit_module.AuditReport()
        audit_module.retry_incomplete(session, config, secrets, result, logger)
        from sentinela.pipeline import RunReport, dispatch

        stub = RunReport(run_id="retry")
        dispatch(session, config, secrets, stub, logger, limit=limit)
    console.print(
        f"[green]Reprocessados {result.documents_retried} documento(s), "
        f"recuperados {result.documents_recovered}. "
        f"{stub.notifications_sent} notificação(ões) entregue(s).[/green]"
    )


@app.command("test-notification")
def test_notification(
    channel: Annotated[str | None, typer.Option(help="Canal específico.")] = None,
) -> None:
    """Envia uma mensagem de teste por cada canal habilitado."""
    from sentinela.notifications import make_notifiers

    config = load_config()
    secrets = Secrets()
    stamp = now().astimezone(config.zone).strftime("%d/%m/%Y %H:%M")
    body = (
        "Sentinela AC — teste de canal\n\n"
        f"Se você recebeu esta mensagem, o canal está funcionando.\n"
        f"Enviado em {stamp} ({config.profile.timezone}).\n\n"
        "Esta mensagem não indica nenhum concurso."
    )
    adapters = [a for a in make_notifiers(config, secrets) if channel in (None, a.name)]
    if not adapters:
        _fail("Nenhum canal habilitado corresponde ao pedido.")
    table = Table(title="Teste de notificação", show_header=False)
    failures = 0
    for adapter in adapters:
        result = adapter.send(f"test:{now().isoformat()}", body)
        colour = "green" if result.status == "SENT" else "red"
        failures += result.status not in ("SENT",)
        table.add_row(
            adapter.name, f"[{colour}]{result.status}[/{colour}]", result.error or "entregue"
        )
    console.print(table)
    if failures:
        raise typer.Exit(code=1)


@app.command()
def bot(
    watch: Annotated[
        bool, typer.Option(help="Ficar escutando (resposta imediata). Ctrl+C encerra.")
    ] = False,
    rounds: Annotated[int, typer.Option(help="Ciclos no modo escuta. 0 = sem limite.")] = 0,
    minutes: Annotated[
        float, typer.Option(help="Minutos de escuta antes de encerrar. 0 = sem limite.")
    ] = 0,
) -> None:
    """Responde as mensagens que chegaram no Telegram."""
    from sentinela.bot import poll_once, publish_menu

    configure("INFO")
    config, secrets, engine = _context()
    if not secrets.telegram_bot_token.get_secret_value():
        _fail("TELEGRAM_BOT_TOKEN não configurado: não há o que escutar.")
    factory = session_factory(engine)

    def ciclo(espera: int) -> int:
        with session_scope(factory) as session:
            outcome = poll_once(session, config, secrets, timeout=espera)
        if outcome.received:
            console.print(
                f"[green]{outcome.answered} respondida(s)[/green] · "
                f"{outcome.ignored} ignorada(s) · {outcome.errors} erro(s)"
            )
        return outcome.answered

    if not watch:
        total = ciclo(0)
        if not total:
            console.print("[dim]Nada novo para responder.[/dim]")
        return

    if publish_menu(secrets):
        console.print("[dim]Menu de comandos publicado no Telegram.[/dim]")
    console.print("[green]Escutando o Telegram. Ctrl+C encerra.[/green]")
    volta = 0
    # Um job na nuvem precisa terminar sozinho para o proximo turno assumir. O
    # Telegram guarda o que nao foi confirmado por 24h, entao um intervalo entre
    # turnos atrasa a resposta, nunca perde a mensagem.
    limite = time.monotonic() + minutes * 60 if minutes > 0 else None
    try:
        while (rounds == 0 or volta < rounds) and (limite is None or time.monotonic() < limite):
            # Long polling: a resposta sai em segundos, sem ficar batendo na API.
            ciclo(25)
            volta += 1
    except KeyboardInterrupt:
        console.print("\n[dim]Encerrado.[/dim]")


@app.command()
def watchdog(
    notify: Annotated[bool, typer.Option(help="Enfileirar alerta se houver problema.")] = True,
) -> None:
    """Verifica se o próprio monitoramento continua vivo."""
    config, secrets, engine = _context()
    with session_scope(session_factory(engine)) as session:
        verdict = watchdog_module.evaluate(session, config)
        if notify:
            watchdog_module.notify(session, verdict, config, secrets)
            from sentinela.pipeline import RunReport, dispatch

            dispatch(
                session,
                config,
                secrets,
                RunReport(run_id="watchdog"),
                RunLogger(get("watchdog"), {}),
            )
    status = watchdog_module.ping(secrets, watchdog_module.heartbeat_payload(verdict))
    colour = {"OK": "green", "WARNING": "yellow"}.get(verdict.severity, "red")
    console.print(f"[{colour}]{verdict.severity}[/{colour}] — {verdict.title}\n{verdict.detail}")
    if status != "SKIPPED":
        console.print(f"[dim]Ping externo: {status}[/dim]")
    if verdict.severity == "CRITICAL":
        raise typer.Exit(code=1)


@app.command()
def backup(
    directory: Annotated[Path, typer.Option(help="Destino dos arquivos.")] = Path("backups"),
    keep_days: Annotated[int, typer.Option(help="Retenção em dias.")] = 30,
) -> None:
    """Gera, verifica e poda os backups do banco."""
    _, _, engine = _context()
    result = backup_module.dump(engine, directory)
    ok, detail = backup_module.verify(result.path)
    removed = backup_module.prune(directory, keep_days)
    table = Table(title="Backup", show_header=False)
    table.add_row("Arquivo", str(result.path))
    table.add_row("Método", result.method)
    table.add_row("Tamanho", f"{result.bytes / 1024:.0f} KiB")
    table.add_row("SHA-256", result.sha256[:32] + "…")
    table.add_row(
        "Verificação", f"[{'green' if ok else 'red'}]{'OK' if ok else 'FALHOU'}[/] {detail}"
    )
    table.add_row("Removidos por retenção", str(len(removed)))
    console.print(table)
    if not ok:
        raise typer.Exit(code=1)


@app.command()
def schedule() -> None:
    """Mostra os horários de execução convertidos para UTC."""
    from sentinela.pipeline import utc_schedule

    config = load_config()
    console.print(
        f"Primária {config.monitoring.primary_local_time} "
        f"({config.profile.timezone}) → cron UTC "
        f"`{utc_schedule(config.monitoring.primary_local_time, config.profile.timezone)}`\n"
        f"Backup   {config.monitoring.backup_local_time} "
        f"({config.profile.timezone}) → cron UTC "
        f"`{utc_schedule(config.monitoring.backup_local_time, config.profile.timezone)}`"
    )


def main() -> None:
    try:
        app()
    except DatabaseUnavailable as error:
        console.print(f"[bold red]{error}[/bold red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
