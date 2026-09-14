"""Structured logging with secret redaction.

Logs are the only thing a person reads when a run goes wrong at 07:17, so every line
carries run_id / source_id / document_id when they exist. Nothing that could be a token is
ever emitted, even if a caller passes one by mistake.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

SECRET_ENV = (
    "DATABASE_URL",
    "TEST_DATABASE_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SMTP_PASSWORD",
    "LLM_API_KEY",
    "GITHUB_TOKEN",
    "BACKUP_ENCRYPTION_KEY",
    "WATCHDOG_PING_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_ANON_KEY",
)
# Belt and braces: even an unset-but-leaked token shape is masked.
_PATTERNS = (
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
    re.compile(r"\b(?:sk|rk|gh[pousr])[-_][A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:@\s]+:)[^@\s]+(@)"),
    re.compile(r"(?i)\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)
MASK = "[REDACTED]"


def redact(value: str) -> str:
    text = str(value)
    for name in SECRET_ENV:
        secret = os.environ.get(name)
        if secret and len(secret) >= 6 and secret in text:
            text = text.replace(secret, MASK)
    for pattern in _PATTERNS:
        text = pattern.sub(
            lambda match: (
                (match[1] + MASK + match[2]) if match.groups() and match.lastindex == 2 else MASK
            ),
            text,
        )
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in ("run_id", "source_id", "document_id", "request_id", "stage", "detail"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = redact(str(value)) if isinstance(value, str) else value
        if record.exc_info:
            payload["error"] = redact(self.formatException(record.exc_info))[:2000]
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        moment = datetime.fromtimestamp(record.created, UTC).strftime("%H:%M:%S")
        context = " ".join(
            f"{key}={getattr(record, key)}"
            for key in ("source_id", "document_id", "stage")
            if getattr(record, key, None)
        )
        line = f"{moment} {record.levelname:<7} {redact(record.getMessage())}"
        return f"{line}  {context}".rstrip()


def configure(level: str = "INFO", json_output: bool | None = None) -> logging.Logger:
    if json_output is None:
        # GitHub Actions keeps the JSON; a terminal gets something readable.
        json_output = bool(os.environ.get("GITHUB_ACTIONS"))
    root = logging.getLogger("sentinela")
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False
    return root


def get(name: str = "sentinela") -> logging.Logger:
    return logging.getLogger(name if name.startswith("sentinela") else f"sentinela.{name}")


class RunLogger(logging.LoggerAdapter):  # type: ignore[type-arg]
    """Attaches the run_id to every line without each call site repeating it."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        extra = dict(self.extra or {})
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs

    def child(self, **context: Any) -> RunLogger:
        return RunLogger(self.logger, {**(self.extra or {}), **context})
