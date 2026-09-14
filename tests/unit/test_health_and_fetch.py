"""Health, circuit breaker, drift detection and HTTP fault injection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from sentinela import health
from sentinela.collector import SourceOutcome, relevant, safe_collect
from sentinela.domain import SourceSpec
from sentinela.extract import extract, extract_pdf
from sentinela.fetch import Fetcher, canonical_url, guess_media_type, host_allowed

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------- URL handling


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://Exemplo.GOV.br/Editais/?b=2&a=1#topo", "https://exemplo.gov.br/Editais?a=1&b=2"),
        ("https://x.gov.br/a/?utm_source=news&id=7", "https://x.gov.br/a?id=7"),
        ("https://x.gov.br/", "https://x.gov.br/"),
        ("javascript:alert(1)", ""),
        ("mailto:alguem@x.gov.br", ""),
        ("file:///etc/passwd", ""),
    ],
)
def test_canonical_url(raw: str, expected: str) -> None:
    assert canonical_url(raw) == expected


def test_relative_links_resolve_against_the_page() -> None:
    assert (
        canonical_url("../edital.pdf", base="https://x.gov.br/a/b/index.html")
        == "https://x.gov.br/a/edital.pdf"
    )


@pytest.mark.parametrize(
    "url,allowed,expected",
    [
        ("https://www.tjac.jus.br/x", ["www.tjac.jus.br"], True),
        ("https://sub.tjac.jus.br/x", ["tjac.jus.br"], True),
        ("https://tjac.jus.br.evil.com/x", ["tjac.jus.br"], False),
        ("https://eviltjac.jus.br/x", ["tjac.jus.br"], False),
    ],
)
def test_host_allow_list_cannot_be_spoofed(url: str, allowed: list[str], expected: bool) -> None:
    assert host_allowed(url, allowed) is expected


def test_media_type_detection_trusts_content_over_headers() -> None:
    assert guess_media_type("https://x/a", "text/html", b"%PDF-1.7 ...") == "pdf"
    assert guess_media_type("https://x/a.pdf", "application/octet-stream", b"x") == "pdf"
    assert guess_media_type("https://x/a", "text/html; charset=utf-8", b"<html>") == "html"


# ---------------------------------------------------------------- fault injection


def transport(*responses: object) -> httpx.MockTransport:
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]

    return httpx.MockTransport(handle)


def fetcher(*responses: object, attempts: int = 3) -> Fetcher:
    client = httpx.Client(transport=transport(*responses), follow_redirects=True)
    return Fetcher(client=client, max_attempts=attempts, min_interval=0, sleeper=lambda _: None)


def test_http_500_is_retried_then_reported() -> None:
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    with Fetcher(
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        max_attempts=3,
        min_interval=0,
        sleeper=lambda _: None,
    ) as client:
        response = client.get("https://x.gov.br/a")
    assert response.status == 500
    assert response.ok is False
    assert len(calls) == 3  # retried, but bounded


def test_http_403_is_not_retried() -> None:
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403)

    with Fetcher(
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        max_attempts=3,
        min_interval=0,
        sleeper=lambda _: None,
    ) as client:
        assert client.get("https://x.gov.br/a").status == 403
    assert len(calls) == 1  # a refusal is respected, not hammered


def test_retry_after_is_honoured() -> None:
    waits: list[float] = []
    responses = [httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(200, text="ok")]
    index = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        response = responses[min(index["n"], len(responses) - 1)]
        index["n"] += 1
        return response

    with Fetcher(
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        max_attempts=3,
        min_interval=0,
        sleeper=waits.append,
    ) as client:
        assert client.get("https://x.gov.br/a").status == 200
    assert 7.0 in waits


def test_timeout_becomes_an_error_not_an_exception() -> None:
    with fetcher(
        httpx.ReadTimeout("slow", request=httpx.Request("GET", "https://x.gov.br"))
    ) as client:
        response = client.get("https://x.gov.br/a")
    assert response.status is None
    assert response.error == "ReadTimeout"
    assert response.ok is False


def test_conditional_request_returns_unchanged() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["If-None-Match"] == 'W/"abc"'
        return httpx.Response(304)

    with Fetcher(
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        min_interval=0,
        sleeper=lambda _: None,
    ) as client:
        response = client.get("https://x.gov.br/a", etag='W/"abc"')
    assert response.unchanged is True
    assert response.from_cache is True


def test_oversized_document_is_refused_before_download() -> None:
    with Fetcher(
        client=httpx.Client(
            transport=transport(
                httpx.Response(200, headers={"content-length": "99999999"}, content=b"x")
            )
        ),
        min_interval=0,
        max_bytes=1024,
        sleeper=lambda _: None,
    ) as client:
        response = client.get("https://x.gov.br/a.pdf")
    assert response.content == b""
    assert "limite" in (response.error or "")


def test_body_without_declared_length_is_truncated_while_streaming() -> None:
    # No content-length header, so the cap can only be enforced chunk by chunk.
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=httpx.ByteStream(b"y" * 5000), headers={"transfer-encoding": "chunked"}
        )

    with Fetcher(
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        min_interval=0,
        max_bytes=1000,
        sleeper=lambda _: None,
    ) as client:
        response = client.get("https://x.gov.br/a")
    assert 1000 < len(response.content) <= 5000
    assert "truncado" in (response.error or "")


# ---------------------------------------------------------------- broken documents


def test_invalid_html_never_raises() -> None:
    document = extract(
        b"<html><body><p>sem fechar<div><<>", "https://x.gov.br", "html", allowed_hosts=["x.gov.br"]
    )
    assert document.status in ("OK", "EMPTY")


def test_malformed_pdf_is_reported_not_raised() -> None:
    document = extract_pdf(b"%PDF-1.4\nlixo binario nao estruturado", allow_ocr=False)
    assert document.usable is False
    assert document.status in ("PARSER_ERROR", "NO_TEXT_LAYER", "EMPTY")


def test_scanned_pdf_without_text_layer_goes_to_review() -> None:
    import pymupdf

    document = pymupdf.open()
    document.new_page()
    empty = document.tobytes()
    document.close()
    result = extract_pdf(empty, allow_ocr=False)
    assert result.usable is False
    assert result.status in ("NO_TEXT_LAYER", "EMPTY")


def test_links_outside_the_allow_list_are_dropped() -> None:
    html = b'<a href="https://x.gov.br/e.pdf">ok</a><a href="https://mal.com/e.pdf">nao</a>'
    document = extract(html, "https://x.gov.br/", "html", allowed_hosts=["x.gov.br"])
    assert [url for url, _ in document.links] == ["https://x.gov.br/e.pdf"]


def test_link_relevance_filter() -> None:
    import re

    pattern = re.compile(r"(?i)concurso|edital|seletiv|\.pdf(?:$|\?)")
    assert relevant("https://x.gov.br/edital-01.pdf", "Edital", pattern) is True
    assert relevant("https://x.gov.br/login", "Entrar", pattern) is False
    assert relevant("https://x.gov.br/noticias", "Notícias", pattern) is False


def test_a_broken_source_never_raises_into_the_run_loop() -> None:
    spec = SourceSpec(
        id="quebrada",
        name="q",
        institution="i",
        base_url="https://x.gov.br/",
        allowed_hosts=["x.gov.br"],
        link_pattern="([unbalanced",
    )
    outcome = safe_collect(
        spec,
        Fetcher(
            client=httpx.Client(transport=transport(httpx.Response(200, text="<html></html>")))
        ),
    )
    assert isinstance(outcome, SourceOutcome)
    assert outcome.status == "FAILED"
    assert outcome.error


# ---------------------------------------------------------------- drift and circuit


def test_zero_documents_after_a_healthy_history_is_drift_not_emptiness() -> None:
    verdict = health.detect_drift(
        documents_found=0,
        history=[12, 15, 11, 14],
        previous_fingerprint="a",
        current_fingerprint="b",
        expected_min=0,
        http_status=200,
    )
    assert verdict.detected is True
    assert "0 documentos" in verdict.reason


def test_a_sharp_drop_is_drift() -> None:
    verdict = health.detect_drift(
        documents_found=1,
        history=[20, 22, 19, 21],
        previous_fingerprint="a",
        current_fingerprint="a",
        expected_min=0,
        http_status=200,
    )
    assert verdict.detected is True


def test_a_first_run_with_no_documents_is_not_drift() -> None:
    verdict = health.detect_drift(
        documents_found=0,
        history=[],
        previous_fingerprint=None,
        current_fingerprint="a",
        expected_min=0,
        http_status=200,
    )
    assert verdict.detected is False


def test_an_http_failure_is_not_drift() -> None:
    verdict = health.detect_drift(
        documents_found=0,
        history=[10, 10, 10],
        previous_fingerprint="a",
        current_fingerprint=None,
        expected_min=0,
        http_status=503,
    )
    assert verdict.detected is False


def test_circuit_opens_after_the_threshold_and_cools_down() -> None:
    transition = health.next_state(
        current="DEGRADED",
        success=False,
        consecutive_failures=3,
        drift=False,
        failure_threshold=3,
        cooldown_hours=6,
        now=NOW,
    )
    assert transition.state == "OPEN"
    assert transition.open_until == NOW + timedelta(hours=6)
    assert transition.should_alert is True

    skip, why = health.should_skip("OPEN", transition.open_until, now=NOW + timedelta(hours=1))
    assert skip is True and "Circuito aberto" in why

    skip, _ = health.should_skip("OPEN", transition.open_until, now=NOW + timedelta(hours=7))
    assert skip is False  # cooling period elapsed: probe the source again


def test_success_closes_the_circuit() -> None:
    transition = health.next_state(
        current="OPEN",
        success=True,
        consecutive_failures=0,
        drift=False,
        failure_threshold=3,
        cooldown_hours=6,
        now=NOW,
    )
    assert transition.state == "HEALTHY"


def test_success_with_drift_degrades_instead_of_healing() -> None:
    transition = health.next_state(
        current="HEALTHY",
        success=True,
        consecutive_failures=0,
        drift=True,
        failure_threshold=3,
        cooldown_hours=6,
        now=NOW,
    )
    assert transition.state == "DEGRADED"


def test_disabled_sources_stay_disabled() -> None:
    transition = health.next_state(
        current="DISABLED",
        success=True,
        consecutive_failures=0,
        drift=False,
        failure_threshold=3,
        cooldown_hours=6,
        now=NOW,
    )
    assert transition.state == "DISABLED"
    assert health.should_skip("DISABLED", None)[0] is True


def test_history_is_bounded_and_round_trips() -> None:
    history: list[str] = []
    for value in range(40):
        history = health.push_history(history, value)
    assert len(history) == health.HISTORY_LENGTH
    assert health.read_history(history)[-1] == 39
    assert health.read_history(["a", None, "3"]) == [3]


def test_fingerprint_ignores_content_and_tracks_structure() -> None:
    a = health.fingerprint({"links": 30, "tables": 2, "title": "Editais", "length": 9000})
    b = health.fingerprint({"links": 30, "tables": 2, "title": "Editais 2026", "length": 12000})
    c = health.fingerprint({"links": 3, "tables": 0, "title": "Editais", "length": 9000})
    assert a == b  # new items published, same page structure
    assert a != c  # the page itself changed shape


def test_parser_rate_handles_zero_attempts() -> None:
    assert health.parser_rate(0, 0) == 0.0
    assert health.parser_rate(3, 4) == 0.75
