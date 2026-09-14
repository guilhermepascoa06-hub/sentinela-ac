"""HTTP layer. Polite, bounded, and incapable of hanging a monitoring run.

Everything downloaded here is untrusted data: it is never executed, never used to build a
filesystem path, and never followed outside the host allow-list of its own source.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36 SentinelaAC/1.0 (+monitor de concursos publicos)"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}
# Retrying these is pointless or hostile; 429/5xx/408 are the only ones worth a second try.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 507, 509}
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "msclkid",
    "_ga",
    "ref",
    "origin",
}


@dataclass(slots=True)
class Response:
    url: str
    status: int | None
    content: bytes
    media_type: str
    headers: dict[str, str]
    elapsed_ms: int
    from_cache: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def unchanged(self) -> bool:
        return self.status == 304


def canonical_url(url: str, base: str | None = None) -> str:
    """Stable identity for a URL: no fragment, no tracking noise, sorted query."""
    absolute = urljoin(base, url) if base else url
    parts = urlparse(absolute.strip())
    if parts.scheme not in ("http", "https"):
        return ""
    query = "&".join(
        sorted(
            piece
            for piece in parts.query.split("&")
            if piece and piece.split("=")[0].lower() not in TRACKING_PARAMS
        )
    )
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((parts.scheme, parts.netloc.lower(), path, "", query, ""))


def host_allowed(url: str, allowed: list[str]) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    if not host:
        return False
    return any(
        host == entry or host.endswith("." + entry) for entry in (h.lower() for h in allowed)
    )


def guess_media_type(url: str, content_type: str, content: bytes) -> str:
    if content.startswith(b"%PDF-"):
        return "pdf"
    base = content_type.split(";")[0].strip().lower()
    if "pdf" in base or urlparse(url).path.lower().endswith(".pdf"):
        return "pdf"
    if "json" in base:
        return "json"
    if "xml" in base or base.endswith("+xml"):
        return "xml"
    if base.startswith("text/") or "html" in base:
        return "html"
    return base or "unknown"


@dataclass
class Fetcher:
    """One instance per monitoring run. Rate limiting is tracked per host, not globally."""

    timeout: float = 25.0
    max_attempts: int = 3
    min_interval: float = 1.5
    max_bytes: int = 12 * 1024 * 1024
    client: httpx.Client | None = None
    sleeper: Any = time.sleep
    _last_call: dict[str, float] = field(default_factory=dict, init=False)
    _owned: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self._owned = True
            self.client = httpx.Client(
                timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
                headers=DEFAULT_HEADERS,
                follow_redirects=True,
                max_redirects=5,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                # TLS verification is never disabled: a government site with a broken
                # certificate is a degraded source, not a reason to lower our guarantees.
                verify=True,
            )

    def close(self) -> None:
        if self._owned and self.client is not None:
            self.client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        previous = self._last_call.get(host)
        if previous is not None:
            wait = self.min_interval - (time.monotonic() - previous)
            if wait > 0:
                self.sleeper(wait)
        self._last_call[host] = time.monotonic()

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                # Honour the server's own number before our exponential guess.
                return max(0.0, min(float(retry_after), 60.0))
            except ValueError:
                pass
        return min(2.0**attempt, 20.0) + random.uniform(0, 0.75)  # noqa: S311 - jitter only

    def get(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> Response:
        assert self.client is not None
        target = canonical_url(url)
        if not target:
            return Response(url, None, b"", "unknown", {}, 0, error="URL nao suportada")
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        last: Response | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._throttle(target)
            started = time.monotonic()
            try:
                with self.client.stream("GET", target, headers=headers) as stream:
                    elapsed = int((time.monotonic() - started) * 1000)
                    if stream.status_code == 304:
                        return Response(
                            str(stream.url),
                            304,
                            b"",
                            "unknown",
                            dict(stream.headers),
                            elapsed,
                            from_cache=True,
                        )
                    declared = stream.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        return Response(
                            str(stream.url),
                            stream.status_code,
                            b"",
                            "unknown",
                            dict(stream.headers),
                            elapsed,
                            error=f"Documento acima do limite ({declared} bytes)",
                        )
                    body = bytearray()
                    truncated = False
                    for chunk in stream.iter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_bytes:
                            truncated = True
                            break
                    elapsed = int((time.monotonic() - started) * 1000)
                    content = bytes(body)
                    response = Response(
                        str(stream.url),
                        stream.status_code,
                        content,
                        guess_media_type(
                            str(stream.url), stream.headers.get("content-type", ""), content
                        ),
                        dict(stream.headers),
                        elapsed,
                        error="Documento truncado no limite de tamanho" if truncated else None,
                    )
            except httpx.HTTPError as error:
                response = Response(
                    target,
                    None,
                    b"",
                    "unknown",
                    {},
                    int((time.monotonic() - started) * 1000),
                    error=type(error).__name__,
                )
            last = response
            retryable = response.status in RETRYABLE_STATUS or response.status is None
            if not retryable or attempt == self.max_attempts:
                return response
            self.sleeper(self._backoff(attempt, response.headers.get("retry-after")))
        assert last is not None
        return last
