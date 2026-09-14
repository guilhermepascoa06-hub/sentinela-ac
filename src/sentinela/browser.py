"""Browser rendering, used only when plain HTTP genuinely cannot see the page.

Some official portals render their edital list with JavaScript, and some sit behind a
front door that a bare HTTP client never gets through. For those, a real browser is the
only honest way to see what any citizen sees when they open the page.

What this module will not do, ever:

  * solve a CAPTCHA, or click past one;
  * log in, or carry anyone's credentials;
  * download or execute a file the page offers;
  * follow a link outside the source's own allow-list.

A page that answers with a CAPTCHA or a login wall is reported as blocked and the source
stays degraded. That is the correct outcome: the information is not public to us, and
pretending otherwise would be both dishonest and fragile.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from sentinela.fetch import DEFAULT_HEADERS, USER_AGENT, Response, canonical_url
from sentinela.logging import get

logger = get("browser")

# Rendering is slow and heavy: it is a last resort, never the default path.
DEFAULT_TIMEOUT_MS = 45_000
SETTLE_MS = 2_500
MAX_PAGES_PER_RUN = 12

# Statuses where a real browser plausibly succeeds when httpx did not: a JS challenge, a
# TLS/header fingerprint filter, or a server that only answers a full browser handshake.
RETRY_WITH_BROWSER = frozenset({403, 429, 503, 520, 521, 522, 525, 526})

_WALL_MARKERS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "cf-challenge",
    "verifique que voce nao e um robo",
    "verifying you are human",
    "checking your browser",
    "faca login para continuar",
    "acesso restrito",
)


@dataclass
class BrowserFetcher:
    """One browser per monitoring run, shared across the sources that need it."""

    timeout_ms: int = DEFAULT_TIMEOUT_MS
    max_bytes: int = 12 * 1024 * 1024
    min_interval: float = 1.5
    max_pages: int = MAX_PAGES_PER_RUN
    sleeper: Any = time.sleep
    _playwright: Any = field(default=None, init=False)
    _browser: Any = field(default=None, init=False)
    _context: Any = field(default=None, init=False)
    _pages_used: int = field(default=0, init=False)
    _last_call: dict[str, float] = field(default_factory=dict, init=False)
    unavailable_reason: str = field(default="", init=False)

    # ------------------------------------------------------------------ lifecycle

    @property
    def exhausted(self) -> bool:
        return self._pages_used >= self.max_pages

    def _ensure(self) -> bool:
        """Start the browser on first real use. Never raises."""
        if self._context is not None:
            return True
        if self.unavailable_reason:
            return False
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.unavailable_reason = "Playwright nao instalado"
            return False
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
            )
            self._context = self._browser.new_context(
                locale="pt-BR",
                timezone_id="America/Rio_Branco",
                user_agent=USER_AGENT,
                extra_http_headers={
                    key: value for key, value in DEFAULT_HEADERS.items() if key != "User-Agent"
                },
                viewport={"width": 1366, "height": 900},
                java_script_enabled=True,
                # Nothing the page offers is ever saved to disk.
                accept_downloads=False,
            )
            self._context.set_default_timeout(self.timeout_ms)
        except Exception as error:  # noqa: BLE001 - a missing browser binary must not stop a run
            self.unavailable_reason = f"Navegador indisponivel: {type(error).__name__}"
            self.close()
            return False
        return True

    def close(self) -> None:
        for resource, name in ((self._context, "_context"), (self._browser, "_browser")):
            if resource is not None:
                try:
                    resource.close()
                except Exception:  # noqa: BLE001,S110  # nosec B110
                    # Teardown of a browser that may already be gone. Failing here would
                    # mask whatever real error is unwinding the stack.
                    pass
                setattr(self, name, None)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001,S110  # nosec B110
                pass  # same: shutting down a dead driver is not an error worth raising
            self._playwright = None

    def __enter__(self) -> BrowserFetcher:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ helpers

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        previous = self._last_call.get(host)
        if previous is not None:
            wait = self.min_interval - (time.monotonic() - previous)
            if wait > 0:
                self.sleeper(wait)
        self._last_call[host] = time.monotonic()

    @staticmethod
    def _walled(text: str) -> str:
        from sentinela.domain import normalize

        plain = normalize(text[:4000])
        for marker in _WALL_MARKERS:
            if marker in plain:
                return marker
        return ""

    # ------------------------------------------------------------------ rendering

    def render(self, url: str) -> Response:
        """Load `url` in a real browser and return its rendered HTML."""
        target = canonical_url(url)
        if not target:
            return Response(url, None, b"", "unknown", {}, 0, error="URL nao suportada")
        if self.exhausted:
            return Response(
                target,
                None,
                b"",
                "unknown",
                {},
                0,
                error=f"Limite de {self.max_pages} paginas renderizadas por execucao",
            )
        if not self._ensure():
            return Response(target, None, b"", "unknown", {}, 0, error=self.unavailable_reason)

        self._throttle(target)
        self._pages_used += 1
        started = time.monotonic()
        page = None
        try:
            page = self._context.new_page()
            # Images and fonts are pure cost here: we only ever read text and links.
            page.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in ("image", "media", "font")
                    else route.continue_()
                ),
            )
            reply = page.goto(target, wait_until="domcontentloaded", timeout=self.timeout_ms)
            page.wait_for_timeout(SETTLE_MS)
            elapsed = int((time.monotonic() - started) * 1000)
            status = reply.status if reply is not None else None
            headers = dict(reply.headers) if reply is not None else {}

            if wall := self._walled(page.inner_text("body")):
                # Never try to get past it. The page is not public to an automated client.
                return Response(
                    str(page.url),
                    status,
                    b"",
                    "html",
                    headers,
                    elapsed,
                    error=f"Pagina protegida por verificacao ({wall}): nao contornada",
                )

            html = page.content()
            content = html.encode("utf-8", "replace")[: self.max_bytes]
            return Response(str(page.url), status, content, "html", headers, elapsed)
        except Exception as error:  # noqa: BLE001 - one page must not break the run
            return Response(
                target,
                None,
                b"",
                "unknown",
                {},
                int((time.monotonic() - started) * 1000),
                error=f"Render falhou: {type(error).__name__}",
            )
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001,S110  # nosec B110
                    pass  # the page is already gone; the response is what matters

    def fetch(self, url: str) -> Response:
        """Fetch a document through the browser context.

        Documents behind the same front door as the listing need the cookies and TLS
        fingerprint the browser already established, so a plain HTTP client would be
        refused again. The bytes are returned as data and never written to disk.
        """
        target = canonical_url(url)
        if not target:
            return Response(url, None, b"", "unknown", {}, 0, error="URL nao suportada")
        if not self._ensure():
            return Response(target, None, b"", "unknown", {}, 0, error=self.unavailable_reason)
        self._throttle(target)
        started = time.monotonic()
        try:
            reply = self._context.request.get(target, timeout=self.timeout_ms)
            elapsed = int((time.monotonic() - started) * 1000)
            body = reply.body()
            truncated = len(body) > self.max_bytes
            headers = dict(reply.headers)
            from sentinela.fetch import guess_media_type

            content = body[: self.max_bytes]
            return Response(
                str(reply.url),
                reply.status,
                content,
                guess_media_type(str(reply.url), headers.get("content-type", ""), content),
                headers,
                elapsed,
                error="Documento truncado no limite de tamanho" if truncated else None,
            )
        except Exception as error:  # noqa: BLE001
            return Response(
                target,
                None,
                b"",
                "unknown",
                {},
                int((time.monotonic() - started) * 1000),
                error=f"Download via navegador falhou: {type(error).__name__}",
            )


def should_escalate(response: Response, candidates: int, forced: bool) -> bool:
    """Decide whether a browser is worth trying after a plain HTTP attempt.

    Escalate when the door was shut in a way a browser might legitimately open, or when
    the page answered fine but yielded nothing -- the signature of a list built by
    JavaScript. Never escalate on 404, 401 or 500: those are answers, not doors.
    """
    if forced:
        return True
    if response.status in RETRY_WITH_BROWSER:
        return True
    if response.status is None and response.error:  # timeout or connection failure
        return True
    return bool(response.ok and candidates == 0)
