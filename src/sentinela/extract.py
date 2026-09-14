"""Turn downloaded bytes into text and links.

Fallback chain, in order, stopping at the first usable result:
    HTML structured content -> PDF native text (PyMuPDF) -> alternative PDF parser (pypdf)
    -> browser retrieval (optional) -> OCR (optional) -> semantic AI -> manual-review queue.

Anything that fails the whole chain becomes REVIEW, never silence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from sentinela.fetch import canonical_url, host_allowed

MAX_TEXT_CHARS = 400_000
_WHITESPACE = re.compile(r"[ \t   ]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass(slots=True)
class ExtractedDocument:
    text: str = ""
    title: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)
    page_count: int | None = None
    method: str = "none"
    status: str = "EMPTY"
    truncated: bool = False
    detail: str = ""
    fingerprint: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.status == "OK" and len(self.text.strip()) >= 80


def tidy(text: str) -> tuple[str, bool]:
    cleaned = _BLANK_LINES.sub("\n\n", _WHITESPACE.sub(" ", text.replace("\r\n", "\n")))
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n")).strip()
    if len(cleaned) > MAX_TEXT_CHARS:
        return cleaned[:MAX_TEXT_CHARS], True
    return cleaned, False


def extract_html(content: bytes, url: str, allowed_hosts: list[str]) -> ExtractedDocument:
    try:
        soup = BeautifulSoup(content, "html.parser")
    except Exception as error:  # noqa: BLE001 - malformed markup must not crash a run
        return ExtractedDocument(method="html", status="PARSER_ERROR", detail=type(error).__name__)
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    title = (soup.title.get_text(strip=True) if soup.title else "")[:300]
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = canonical_url(str(anchor["href"]), base=url)
        if not href or href in seen:
            continue
        if allowed_hosts and not host_allowed(href, allowed_hosts):
            continue
        seen.add(href)
        links.append((href, anchor.get_text(" ", strip=True)[:300]))
    text, truncated = tidy(soup.get_text("\n"))
    # The fingerprint is what schema-drift detection compares between runs.
    fingerprint = {
        "links": len(soup.find_all("a", href=True)),
        "tables": len(soup.find_all("table")),
        "lists": len(soup.find_all(["ul", "ol"])),
        "headings": len(soup.find_all(["h1", "h2", "h3"])),
        "title": title,
        "length": len(text),
    }
    return ExtractedDocument(
        text=text,
        title=title,
        links=links,
        method="html",
        status="OK" if text.strip() else "EMPTY",
        truncated=truncated,
        fingerprint=fingerprint,
    )


def _pymupdf(content: bytes) -> ExtractedDocument:
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - dependency is pinned
        return ExtractedDocument(method="pdf_pymupdf", status="UNAVAILABLE")
    try:
        with pymupdf.open(stream=content, filetype="pdf") as document:
            if document.is_encrypted and not document.authenticate(""):
                return ExtractedDocument(
                    method="pdf_pymupdf", status="ENCRYPTED", detail="PDF protegido por senha"
                )
            pages = [page.get_text("text") for page in document]
            count = document.page_count
    except Exception as error:  # noqa: BLE001 - a corrupt PDF is expected input
        return ExtractedDocument(
            method="pdf_pymupdf", status="PARSER_ERROR", detail=type(error).__name__
        )
    text, truncated = tidy("\n".join(pages))
    return ExtractedDocument(
        text=text,
        page_count=count,
        method="pdf_pymupdf",
        status="OK" if text.strip() else "NO_TEXT_LAYER",
        truncated=truncated,
        fingerprint={"pages": count, "length": len(text)},
    )


def _pypdf(content: bytes) -> ExtractedDocument:
    try:
        import io

        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return ExtractedDocument(method="pdf_pypdf", status="UNAVAILABLE")
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                return ExtractedDocument(method="pdf_pypdf", status="ENCRYPTED")
        pages = [page.extract_text() or "" for page in reader.pages]
        count = len(reader.pages)
    except Exception as error:  # noqa: BLE001
        return ExtractedDocument(
            method="pdf_pypdf", status="PARSER_ERROR", detail=type(error).__name__
        )
    text, truncated = tidy("\n".join(pages))
    return ExtractedDocument(
        text=text,
        page_count=count,
        method="pdf_pypdf",
        status="OK" if text.strip() else "NO_TEXT_LAYER",
        truncated=truncated,
        fingerprint={"pages": count, "length": len(text)},
    )


def _ocr(content: bytes, max_pages: int) -> ExtractedDocument:
    """Scanned editais only. Skipped silently when Tesseract is not installed."""
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        return ExtractedDocument(method="ocr", status="UNAVAILABLE")
    try:
        with pymupdf.open(stream=content, filetype="pdf") as document:
            if not getattr(pymupdf, "TESSDATA_PREFIX", None):
                return ExtractedDocument(
                    method="ocr", status="UNAVAILABLE", detail="Tesseract nao instalado"
                )
            chunks = []
            for page in list(document)[:max_pages]:
                chunks.append(page.get_textpage_ocr(language="por", full=True).extractText())
            count = document.page_count
    except Exception as error:  # noqa: BLE001
        return ExtractedDocument(method="ocr", status="PARSER_ERROR", detail=type(error).__name__)
    text, truncated = tidy("\n".join(chunks))
    return ExtractedDocument(
        text=text,
        page_count=count,
        method="ocr",
        status="OK" if text.strip() else "NO_TEXT_LAYER",
        truncated=truncated,
    )


def extract_pdf(
    content: bytes, max_pages: int = 180, ocr_pages: int = 12, allow_ocr: bool = True
) -> ExtractedDocument:
    primary = _pymupdf(content)
    if primary.usable:
        return primary
    if primary.status == "ENCRYPTED":
        return primary
    secondary = _pypdf(content)
    if secondary.usable:
        return secondary
    if allow_ocr and primary.status in ("NO_TEXT_LAYER", "PARSER_ERROR"):
        scanned = _ocr(content, ocr_pages)
        if scanned.usable:
            return scanned
        primary.detail = primary.detail or scanned.detail
    # Keep the most informative failure so the review queue says something useful.
    best = primary if primary.status != "PARSER_ERROR" else secondary
    if best.status == "OK":
        best.status = "TOO_SHORT"
    return best if best.status != "EMPTY" else primary


def extract(
    content: bytes,
    url: str,
    media_type: str,
    *,
    allowed_hosts: list[str] | None = None,
    max_pages: int = 180,
    ocr_pages: int = 12,
    allow_ocr: bool = True,
) -> ExtractedDocument:
    if media_type == "pdf" or content.startswith(b"%PDF-"):
        return extract_pdf(content, max_pages, ocr_pages, allow_ocr)
    if media_type in ("html", "xml"):
        return extract_html(content, url, allowed_hosts or [])
    if media_type == "json":
        text, truncated = tidy(content.decode("utf-8", "replace"))
        return ExtractedDocument(
            text=text, method="json", status="OK" if text else "EMPTY", truncated=truncated
        )
    text, truncated = tidy(content.decode("utf-8", "replace"))
    return ExtractedDocument(
        text=text, method="raw", status="OK" if text.strip() else "EMPTY", truncated=truncated
    )
