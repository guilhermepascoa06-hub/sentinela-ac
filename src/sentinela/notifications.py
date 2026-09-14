"""Delivery adapters. The database outbox owns retries and cross-run idempotency.

Telegram and SMTP do not support idempotency keys. An ambiguous delivery is therefore
held as UNCERTAIN for review rather than blindly retried and potentially duplicated.
"""

import hashlib
import os
import smtplib
import ssl
import tempfile
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Literal, Protocol

import httpx

from sentinela.config import Configuration, Secrets

# UNCONFIGURED is deliberately distinct from BLOCKED. BLOCKED means the channel
# rejected THIS message and retrying it is pointless. UNCONFIGURED means the channel
# has no credentials yet -- the message is fine and must go out once they arrive.
DeliveryStatus = Literal["SENT", "RETRY", "UNCERTAIN", "BLOCKED", "UNCONFIGURED"]


@dataclass(frozen=True)
class DeliveryResult:
    status: DeliveryStatus
    external_id: str | None = None
    error: str | None = None
    retry_after: int | None = None


class Notifier(Protocol):
    name: str

    def send(self, key: str, message: str) -> DeliveryResult: ...


class ConsoleNotifier:
    name = "console"

    def send(self, key: str, message: str) -> DeliveryResult:
        print(message)
        return DeliveryResult("SENT", external_id=hashlib.sha256(key.encode()).hexdigest())


class MarkdownNotifier:
    name = "markdown"

    def __init__(self, directory: str | Path = "reports/notifications") -> None:
        self.directory = Path(directory)

    def send(self, key: str, message: str) -> DeliveryResult:
        identity = hashlib.sha256(key.encode()).hexdigest()
        destination = self.directory / f"{identity}.md"
        temporary: str | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                return DeliveryResult("SENT", external_id=identity)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.directory, delete=False, suffix=".tmp"
            ) as stream:
                temporary = stream.name
                stream.write(message + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            return DeliveryResult("SENT", external_id=identity)
        except OSError:
            return DeliveryResult("RETRY", error="Markdown storage unavailable")
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


class TelegramNotifier:
    name = "telegram"

    def __init__(self, secrets: Secrets, client: httpx.Client | None = None) -> None:
        self._token = secrets.telegram_bot_token.get_secret_value()
        self._chat = secrets.telegram_chat_id.get_secret_value()
        self._client = client

    def send(self, key: str, message: str) -> DeliveryResult:
        if not self._token or not self._chat:
            return DeliveryResult("UNCONFIGURED", error="Telegram sem credenciais")
        # One request per notification: splitting creates partial-send ambiguity.
        # Plain text also prevents untrusted document text from injecting markup.
        if len(message.encode("utf-16-le")) // 2 > 4096:
            return DeliveryResult("BLOCKED", error="Telegram message exceeds 4096 UTF-16 units")
        owned = self._client is None
        client = self._client or httpx.Client(timeout=20, follow_redirects=False)
        try:
            response = client.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat, "text": message, "disable_web_page_preview": True},
            )
            try:
                body = response.json()
                if not isinstance(body, dict):
                    return DeliveryResult("UNCERTAIN", error="Telegram response invalid")
            except ValueError:
                return DeliveryResult("UNCERTAIN", error="Telegram response invalid")
            if response.status_code == 429 or body.get("error_code") == 429:
                parameters = body.get("parameters")
                value = parameters.get("retry_after", 60) if isinstance(parameters, dict) else 60
                delay = max(1, min(value, 86400)) if type(value) is int else 60
                return DeliveryResult("RETRY", error="Telegram rate limited", retry_after=delay)
            if response.status_code in (401, 403, 400) or body.get("error_code") in (400, 401, 403):
                return DeliveryResult("BLOCKED", error="Telegram rejected configuration or message")
            if response.status_code >= 500:
                return DeliveryResult("UNCERTAIN", error="Telegram server error after submission")
            result = body.get("result")
            if body.get("ok") is True and isinstance(result, dict) and result.get("message_id"):
                return DeliveryResult("SENT", external_id=str(result["message_id"]))
            return DeliveryResult("UNCERTAIN", error="Telegram did not confirm message delivery")
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return DeliveryResult("RETRY", error="Telegram connection unavailable")
        except httpx.RequestError:
            # Never interpolate exceptions: httpx URLs contain the bot credential.
            return DeliveryResult("UNCERTAIN", error="Telegram delivery acknowledgment unavailable")
        finally:
            if owned:
                client.close()


class EmailNotifier:
    name = "email"

    def __init__(self, secrets: Secrets) -> None:
        self._secrets = secrets

    def send(self, key: str, message: str) -> DeliveryResult:
        cfg = self._secrets
        if not all(
            (
                cfg.smtp_host,
                cfg.email_from,
                cfg.email_to,
                cfg.smtp_username,
                cfg.smtp_password.get_secret_value(),
            )
        ):
            return DeliveryResult("UNCONFIGURED", error="E-mail sem credenciais")
        identity = hashlib.sha256(key.encode()).hexdigest()
        mail = EmailMessage()
        try:
            mail["From"] = cfg.email_from
            mail["To"] = cfg.email_to
            mail["Subject"] = "Sentinela AC — atualização"
            mail["Message-ID"] = f"<{identity}@sentinela.invalid>"
            mail.set_content(message)
        except ValueError:
            return DeliveryResult("BLOCKED", error="Email headers invalid")
        submitted = False
        try:
            with smtplib.SMTP_SSL(
                cfg.smtp_host, cfg.smtp_port, timeout=20, context=ssl.create_default_context()
            ) as client:
                client.login(cfg.smtp_username, cfg.smtp_password.get_secret_value())
                submitted = True
                refused = client.send_message(mail)
                if refused:
                    return DeliveryResult("BLOCKED", error="Email recipient refused")
            return DeliveryResult("SENT", external_id=identity)
        except (
            smtplib.SMTPAuthenticationError,
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
        ):
            return DeliveryResult("BLOCKED", error="Email credentials or recipient rejected")
        except (OSError, smtplib.SMTPException):
            return DeliveryResult(
                "UNCERTAIN" if submitted else "RETRY", error="Email delivery unavailable"
            )


def make_notifiers(config: Configuration, secrets: Secrets) -> list[Notifier]:
    adapters: list[Notifier] = []
    if config.notifications.get("console", False):
        adapters.append(ConsoleNotifier())
    if config.notifications.get("markdown", True):
        adapters.append(
            MarkdownNotifier(
                Path(config.storage.get("reports_directory", "reports")) / "notifications"
            )
        )
    if config.notifications.get("telegram", True):
        adapters.append(TelegramNotifier(secrets))
    if config.notifications.get("email", False):
        adapters.append(EmailNotifier(secrets))
    return adapters


def telegram_health(secrets: Secrets, client: httpx.Client | None = None) -> dict[str, str]:
    """Read-only checks; never send a message or return account details/credentials."""
    token = secrets.telegram_bot_token.get_secret_value()
    chat = secrets.telegram_chat_id.get_secret_value()
    if not token or not chat:
        return {"status": "BLOCKED", "detail": "Telegram credentials not configured"}
    owned = client is None
    active = client or httpx.Client(timeout=15, follow_redirects=False)
    try:
        for method, params in (("getMe", {}), ("getChat", {"chat_id": chat})):
            response = active.get(f"https://api.telegram.org/bot{token}/{method}", params=params)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or body.get("ok") is not True:
                return {"status": "DEGRADED", "detail": "Telegram configuration rejected"}
        return {"status": "OK", "detail": "Bot and destination verified"}
    except (httpx.HTTPError, ValueError):
        return {"status": "DEGRADED", "detail": "Telegram check unavailable"}
    finally:
        if owned:
            active.close()
