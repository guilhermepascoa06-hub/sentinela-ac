"""Browser fallback: when to escalate, and what it must refuse to do."""

from __future__ import annotations

import pytest

from sentinela.browser import BrowserFetcher, should_escalate
from sentinela.fetch import Response


def reply(status: int | None, *, error: str | None = None, body: bytes = b"x") -> Response:
    return Response("https://x.gov.br/", status, body, "html", {}, 10, error=error)


@pytest.mark.parametrize(
    "status,candidates,expected",
    [
        (403, 0, True),  # shut out in a way a real browser may legitimately open
        (429, 0, True),
        (503, 0, True),
        (525, 0, True),  # Cloudflare handshake
        (200, 0, True),  # answered fine but produced nothing: list built by JavaScript
        (200, 5, False),  # plain HTTP already worked
        (404, 0, False),  # an answer, not a door
        (401, 0, False),  # genuinely restricted: a browser changes nothing
        (500, 0, False),
    ],
)
def test_escalation_decision(status: int, candidates: int, expected: bool) -> None:
    assert should_escalate(reply(status), candidates, forced=False) is expected


def test_timeout_escalates() -> None:
    assert should_escalate(reply(None, error="ReadTimeout"), 0, forced=False) is True


def test_render_flag_forces_the_browser_even_on_a_good_answer() -> None:
    assert should_escalate(reply(200), 20, forced=True) is True


@pytest.mark.parametrize(
    "texto",
    [
        "Please complete the CAPTCHA to continue",
        "Verifying you are human. This may take a few seconds.",
        "Checking your browser before accessing",
        "Faça login para continuar",
        "Acesso restrito a servidores",
    ],
)
def test_a_wall_is_recognised_and_never_pushed_through(texto: str) -> None:
    assert BrowserFetcher._walled(texto) != ""


def test_ordinary_edital_text_is_not_mistaken_for_a_wall() -> None:
    texto = (
        "EDITAL Nº 01/2026 — CONCURSO PÚBLICO. As inscrições estarão abertas de "
        "11/09/2026 a 13/10/2026. O candidato deverá acessar o sistema de inscrição."
    )
    assert BrowserFetcher._walled(texto) == ""


def test_missing_playwright_degrades_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def refuse(name: str, *args: object, **kwargs: object):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", refuse)
    fetcher = BrowserFetcher()
    result = fetcher.render("https://x.gov.br/editais")
    assert result.ok is False
    assert "Playwright" in (result.error or "")


def test_page_budget_is_enforced() -> None:
    fetcher = BrowserFetcher(max_pages=0)
    result = fetcher.render("https://x.gov.br/editais")
    assert result.ok is False
    assert "Limite" in (result.error or "")


def test_unsupported_scheme_is_refused() -> None:
    assert BrowserFetcher().render("javascript:alert(1)").error == "URL nao suportada"
