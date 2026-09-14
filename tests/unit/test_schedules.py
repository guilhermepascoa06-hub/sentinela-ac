"""The schedule is part of the product: a bot that answers in an hour is a different bot."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/telegram-bot.yml"


def _number(pattern: str, text: str) -> float:
    match = re.search(pattern, text)
    assert match, f"não encontrei {pattern} no workflow do bot"
    return float(match[1])


def test_the_listening_turn_fits_between_two_scheduled_turns() -> None:
    """Listening longer than the interval leaves two turns polling at once, and Telegram
    answers the second with 409. Listening longer than the job timeout gets the process
    killed mid-answer, every turn."""
    text = WORKFLOW.read_text(encoding="utf-8")
    listening = _number(r"--minutes\s+([0-9.]+)", text)
    interval = _number(r"cron:\s*'\*/([0-9]+) \* \* \* \*'", text)
    timeout = _number(r"timeout-minutes:\s*([0-9]+)", text)
    assert 0 < listening < interval, "o turno de escuta invade o turno seguinte"
    assert listening + 1 < timeout, "o job morre antes de a escuta terminar sozinha"


def test_the_bot_answers_in_minutes_not_hours() -> None:
    """The hourly cadence existed because a private repo had 2.000 Actions minutes a
    month. The repository is public: that ceiling is gone, and so is the reason."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert _number(r"cron:\s*'\*/([0-9]+) \* \* \* \*'", text) <= 15
