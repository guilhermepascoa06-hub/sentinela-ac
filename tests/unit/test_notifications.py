from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from sentinela.config import Configuration, Secrets
from sentinela.notifications import (
    ConsoleNotifier,
    EmailNotifier,
    MarkdownNotifier,
    TelegramNotifier,
    make_notifiers,
    telegram_health,
)


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(
        _env_file=None,
        telegram_bot_token=SecretStr("private-test-token"),
        telegram_chat_id=SecretStr("private-chat"),
    )


def test_markdown_persisted_idempotency_and_path_safety(tmp_path: Path) -> None:
    adapter = MarkdownNotifier(tmp_path)
    first = adapter.send("../../escape", "Original evidence")
    second = MarkdownNotifier(tmp_path).send("../../escape", "Changed message")
    assert first == second
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == "Original evidence\n"


def test_console(capsys: pytest.CaptureFixture[str]) -> None:
    assert ConsoleNotifier().send("key", "Alert").status == "SENT"
    assert capsys.readouterr().out == "Alert\n"


@pytest.mark.parametrize(
    "code,payload,status",
    [
        (200, {"ok": True, "result": {"message_id": 77}}, "SENT"),
        (429, {"ok": False, "parameters": {"retry_after": 137}}, "RETRY"),
        (401, {"ok": False, "description": "private-test-token"}, "BLOCKED"),
        (403, {"ok": False}, "BLOCKED"),
        (500, {"ok": False}, "UNCERTAIN"),
        (200, {"ok": True}, "UNCERTAIN"),
        (200, [], "UNCERTAIN"),
    ],
)
def test_telegram_response(secrets: Secrets, code: int, payload: object, status: str) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(code, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = TelegramNotifier(secrets, client).send("key", "Evidence")
    assert result.status == status
    assert len(requests) == 1
    assert "private-test-token" not in repr(result)
    if code == 429:
        assert result.retry_after == 137


@pytest.mark.parametrize(
    "exception,status",
    [
        (httpx.ReadTimeout, "UNCERTAIN"),
        (httpx.WriteTimeout, "UNCERTAIN"),
        (httpx.ConnectError, "RETRY"),
        (httpx.ConnectTimeout, "RETRY"),
    ],
)
def test_failure_no_automatic_post_retry(
    secrets: Secrets, exception: type[httpx.RequestError], status: str
) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise exception(f"secret URL {request.url}", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = TelegramNotifier(secrets, client).send("key", "Message")
    assert result.status == status
    assert "private-test-token" not in repr(result)
    assert calls == 1


def test_missing_credentials_are_not_a_success() -> None:
    empty = Secrets(
        _env_file=None,
        telegram_bot_token=SecretStr(""),
        telegram_chat_id=SecretStr(""),
        smtp_host="",
    )
    # UNCONFIGURED, not BLOCKED: the message is fine, the credential is missing. A
    # terminal BLOCKED here once stranded two real alerts forever, because the outbox
    # never retries BLOCKED and idempotency prevents regenerating them.
    assert TelegramNotifier(empty).send("key", "message").status == "UNCONFIGURED"
    assert EmailNotifier(empty).send("key", "message").status == "UNCONFIGURED"
    assert telegram_health(empty)["status"] == "BLOCKED"


def test_utf16_telegram_limit(secrets: Secrets) -> None:
    assert TelegramNotifier(secrets).send("key", "😀" * 2049).status == "BLOCKED"


def test_health_read_only(secrets: Secrets) -> None:
    methods = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        methods.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert telegram_health(secrets, client)["status"] == "OK"
    assert methods == ["getMe", "getChat"]


def test_enabled_channels(secrets: Secrets, tmp_path: Path) -> None:
    config = Configuration(
        notifications={"telegram": True, "markdown": True, "console": False, "email": False},
        storage={"reports_directory": str(tmp_path)},
    )
    assert [n.name for n in make_notifiers(config, secrets)] == ["markdown", "telegram"]


def test_invalid_json_is_uncertain(secrets: Secrets) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not json"))
    ) as client:
        assert TelegramNotifier(secrets, client).send("k", "m").status == "UNCERTAIN"
