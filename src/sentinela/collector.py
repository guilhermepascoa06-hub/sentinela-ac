"""Per-source collection: discover candidate documents, fetch them, extract their text.

Failure isolation lives here. Every exit path returns a SourceOutcome; nothing raised by
one source can reach the run loop and abort the other thirty-three.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sentinela.domain import SourceSpec, digest
from sentinela.extract import ExtractedDocument, extract
from sentinela.fetch import Fetcher, Response, canonical_url, host_allowed
from sentinela.logging import get

logger = get("collector")
# Recovery ladder, tried in order when the configured entry point yields nothing.
RECOVERY_PATHS = (
    ("rss", ("/feed", "/rss", "/feed/", "/index.xml", "/rss.xml")),
    ("sitemap", ("/sitemap.xml", "/sitemap_index.xml")),
)
_NOISE = re.compile(
    r"(?i)(?:^|/)(?:login|entrar|webmail|intranet|carrinho|whatsapp|facebook|twitter|instagram"
    r"|youtube|linkedin|mailto:|javascript:|tel:|#)"
)
_SITEMAP_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


@dataclass(slots=True)
class FoundDocument:
    url: str
    title: str
    media_type: str
    content: bytes
    document: ExtractedDocument
    http_status: int | None
    etag: str | None
    last_modified: str | None
    content_hash: str
    fetched_at: datetime
    depth: int = 1
    unchanged: bool = False


@dataclass(slots=True)
class SourceOutcome:
    source_id: str
    status: str  # OK | EMPTY | FAILED | SKIPPED
    documents: list[FoundDocument] = field(default_factory=list)
    http_status: int | None = None
    response_ms: int | None = None
    index_fingerprint: str | None = None
    index_structure: dict[str, Any] = field(default_factory=dict)
    links_seen: int = 0
    recovery_method: str | None = None
    error: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    index_unchanged: bool = False
    rendered: bool = False


def relevant(url: str, label: str, pattern: re.Pattern[str]) -> bool:
    if _NOISE.search(url):
        return False
    return bool(pattern.search(url) or pattern.search(label))


def _index_pages(spec: SourceSpec) -> list[str]:
    pages = [spec.base_url, *spec.seed_urls]
    seen: list[str] = []
    for page in pages:
        target = canonical_url(page)
        if target and target not in seen and host_allowed(target, spec.allowed_hosts):
            seen.append(target)
    return seen


def _sitemap_links(content: bytes, spec: SourceSpec) -> list[tuple[str, str]]:
    body = content.decode("utf-8", "replace")[:400_000]
    links: list[tuple[str, str]] = []
    for match in _SITEMAP_LOC.finditer(body):
        target = canonical_url(match[1])
        if target and host_allowed(target, spec.allowed_hosts):
            links.append((target, ""))
    return links


def _recover(fetcher: Fetcher, spec: SourceSpec) -> tuple[list[tuple[str, str]], str | None]:
    """Try well-known alternative entry points before declaring a source empty."""
    from urllib.parse import urlparse, urlunparse

    parts = urlparse(spec.base_url)
    root = urlunparse((parts.scheme, parts.netloc, "", "", "", ""))
    for method, paths in RECOVERY_PATHS:
        for path in paths:
            response = fetcher.get(root + path)
            if not response.ok or not response.content:
                continue
            if method == "sitemap" or response.media_type == "xml":
                links = _sitemap_links(response.content, spec)
            else:
                links = extract(
                    response.content, response.url, "html", allowed_hosts=spec.allowed_hosts
                ).links
            if links:
                logger.info(
                    "Recuperacao via %s em %s",
                    method,
                    spec.id,
                    extra={"source_id": spec.id, "stage": "recovery"},
                )
                return links, method
    return [], None


def collect(
    spec: SourceSpec,
    fetcher: Fetcher,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    max_pages: int = 180,
    ocr_pages: int = 12,
    allow_ocr: bool = True,
    browser: Any = None,
    budget: int | None = None,
) -> SourceOutcome:
    outcome = SourceOutcome(source_id=spec.id, status="FAILED")
    try:
        pattern = re.compile(spec.link_pattern)
    except re.error as error:
        outcome.error = f"link_pattern invalido: {error}"
        return outcome

    candidates: list[tuple[str, str]] = []
    index_response: Response | None = None
    for position, page in enumerate(_index_pages(spec)):
        conditional = (etag, last_modified) if position == 0 else (None, None)
        response = fetcher.get(page, etag=conditional[0], last_modified=conditional[1])
        if position == 0:
            index_response = response
            outcome.http_status = response.status
            outcome.response_ms = response.elapsed_ms
            outcome.etag = response.headers.get("etag") or etag
            outcome.last_modified = response.headers.get("last-modified") or last_modified
        if response.unchanged:
            outcome.index_unchanged = True
            continue
        if not response.ok:
            continue
        document = extract(
            response.content,
            response.url,
            response.media_type,
            allowed_hosts=spec.allowed_hosts,
            max_pages=max_pages,
            ocr_pages=ocr_pages,
            allow_ocr=allow_ocr,
        )
        if position == 0:
            outcome.index_structure = dict(document.fingerprint)
            outcome.links_seen = len(document.links)
        # A seed URL that is itself a document (a direct PDF) counts as a candidate.
        if response.media_type == "pdf":
            candidates.append((response.url, document.title or page))
        for url, label in document.links:
            if relevant(url, label, pattern):
                candidates.append((url, label))

    if index_response is None:
        outcome.error = "Nenhuma pagina inicial acessivel"
        return outcome
    if not index_response.ok and not index_response.unchanged:
        outcome.error = index_response.error or f"HTTP {index_response.status}"
        return outcome

    if not candidates and not outcome.index_unchanged:
        recovered, method = _recover(fetcher, spec)
        outcome.recovery_method = method
        candidates = [(url, label) for url, label in recovered if relevant(url, label, pattern)]

    # Last rung of the ladder: render the page in a real browser. Only when plain HTTP
    # was shut out in a way a browser might legitimately open, or when the page answered
    # fine but produced nothing -- the signature of a list built by JavaScript.
    if browser is not None and not outcome.index_unchanged:
        from sentinela.browser import should_escalate

        if should_escalate(index_response, len(candidates), spec.render):
            rendered = browser.render(spec.base_url)
            if rendered.ok and rendered.content:
                document = extract(
                    rendered.content, rendered.url, "html", allowed_hosts=spec.allowed_hosts
                )
                found = [
                    (url, label) for url, label in document.links if relevant(url, label, pattern)
                ]
                if found:
                    outcome.rendered = True
                    outcome.recovery_method = "browser"
                    outcome.http_status = rendered.status
                    outcome.index_structure = dict(document.fingerprint)
                    outcome.links_seen = len(document.links)
                    seen_urls = {url for url, _ in candidates}
                    candidates += [item for item in found if item[0] not in seen_urls]
                    logger.info(
                        "Recuperacao via navegador em %s: %d links relevantes",
                        spec.id,
                        len(found),
                        extra={"source_id": spec.id, "stage": "recovery"},
                    )
            elif rendered.error:
                outcome.error = outcome.error or rendered.error

    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url, label in candidates:
        if url in seen:
            continue
        seen.add(url)
        ordered.append((url, label))

    allowance = spec.max_documents if budget is None else min(spec.max_documents, budget)
    for url, label in ordered[:allowance]:
        response = fetcher.get(url)
        if outcome.rendered and not response.ok and browser is not None:
            # The listing only opened in the browser, so its documents sit behind the
            # same door: a plain client is refused there too.
            response = browser.fetch(url)
        if not response.ok or not response.content:
            if response.error or response.status:
                logger.warning(
                    "Documento inacessivel: %s",
                    response.error or response.status,
                    extra={"source_id": spec.id, "document_id": url, "stage": "fetch"},
                )
            continue
        document = extract(
            response.content,
            response.url,
            response.media_type,
            allowed_hosts=spec.allowed_hosts,
            max_pages=max_pages,
            ocr_pages=ocr_pages,
            allow_ocr=allow_ocr,
        )
        outcome.documents.append(
            FoundDocument(
                url=canonical_url(response.url) or url,
                title=document.title or label,
                media_type=response.media_type,
                content=response.content,
                document=document,
                http_status=response.status,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                content_hash=digest(response.content),
                fetched_at=datetime.now(UTC),
            )
        )

    outcome.index_fingerprint = digest(outcome.index_structure) if outcome.index_structure else None
    if outcome.documents:
        outcome.status = "OK"
    elif outcome.index_unchanged:
        outcome.status = "OK"  # 304: the index genuinely has nothing new
    else:
        outcome.status = "EMPTY"
    return outcome


def safe_collect(spec: SourceSpec, fetcher: Fetcher, **kwargs: Any) -> SourceOutcome:
    """The only entry point the run loop uses. Never raises."""
    try:
        return collect(spec, fetcher, **kwargs)
    except Exception as error:  # noqa: BLE001 - isolation is the entire point of this wrapper
        logger.exception(
            "Falha isolada na fonte %s", spec.id, extra={"source_id": spec.id, "stage": "collect"}
        )
        return SourceOutcome(source_id=spec.id, status="FAILED", error=type(error).__name__)
