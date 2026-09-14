"""O agendamento é parte do produto: um bot que responde daqui a uma hora é outro bot."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"
BOT = WORKFLOWS / "telegram-bot.yml"


def _number(pattern: str, text: str) -> float:
    match = re.search(pattern, text)
    assert match, f"não encontrei {pattern} no workflow do bot"
    return float(match[1])


def test_the_turn_ends_by_itself_before_the_job_is_killed() -> None:
    """Um processo de nuvem tem teto de duração. Se a escuta passar do timeout, o job morre
    no meio de uma resposta — e morre assim em todo turno, para sempre."""
    text = BOT.read_text(encoding="utf-8")
    escuta = _number(r"--minutes\s+([0-9.]+)", text)
    timeout = _number(r"timeout-minutes:\s*([0-9]+)", text)
    assert 0 < escuta < timeout, "a escuta não cabe dentro do job"
    # O teto de um job no GitHub é 6h; passar disso é morte certa, não risco.
    assert timeout <= 360


def test_a_single_listener_at_a_time() -> None:
    """Dois getUpdates simultâneos recebem 409. O grupo de concorrência é o que permite
    disparar o workflow de vários lugares sem que dois turnos se atropelem."""
    text = BOT.read_text(encoding="utf-8")
    assert re.search(r"concurrency:\s*\n\s*group:\s*sentinela-bot", text)
    assert "cancel-in-progress: false" in text


def test_listening_does_not_depend_only_on_the_cron() -> None:
    """Medido em produção: o agendador do GitHub nunca disparou este workflow, enquanto
    disparava o monitor e o backup. Um único gatilho deixaria o bot surdo."""
    text = BOT.read_text(encoding="utf-8")
    assert "schedule:" in text
    assert "workflow_run:" in text
    assert "workflow_dispatch:" in text
    assert _number(r"cron:\s*'\*/([0-9]+) \* \* \* \*'", text) <= 30


def test_the_fallback_names_workflows_that_really_exist() -> None:
    """O gatilho reserva casa pelo NOME do workflow. Renomear um deles calaria o bot sem
    erro nenhum: é o defeito clássico daqui, a ponta final desligada em silêncio."""
    text = BOT.read_text(encoding="utf-8")
    bloco = text[text.index("workflow_run:") : text.index("types:")]
    citados = {line.strip(" -\n") for line in bloco.splitlines() if line.strip().startswith("- ")}
    assert citados, "o gatilho reserva não cita nenhum workflow"
    existentes = {
        match[1]
        for arquivo in WORKFLOWS.glob("*.yml")
        if (match := re.match(r"name:\s*(.+)", arquivo.read_text(encoding="utf-8")))
    }
    assert citados <= existentes, f"workflow inexistente no gatilho reserva: {citados - existentes}"
