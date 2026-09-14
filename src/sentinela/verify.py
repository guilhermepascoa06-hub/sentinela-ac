"""Turn a model's claim into evidence, or throw it away.

The architecture rule stands: a language model never decides eligibility. But there is a
real difference between deciding a fact and *finding* it. A 56-page edital may state the
workload once, in prose, in a sentence no table parser will ever see. Asking a model to
point at that sentence is useful; believing what it says about it is not.

So the model is used as a locator, never as an authority:

  1. it must return a quote it claims is literally in the document;
  2. the quote must actually be there -- verified by string comparison, not trust;
  3. the deterministic parser must independently re-derive the value from that quote;
  4. only if the parser agrees does the value enter the system.

A hallucinated sentence fails step 2. A real sentence that does not actually support the
claim fails step 3. What survives is a value that deterministic code extracted, with the
model having done nothing but narrow down where to look.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sentinela.domain import normalize
from sentinela.parse import (
    parse_education,
    parse_money,
    parse_vacancies,
    parse_weekly_workload,
)

# Shortest quote worth checking. Anything smaller matches by accident.
MIN_QUOTE_CHARS = 25
MAX_QUOTE_CHARS = 400

# Fields a model may point at, each with the parser that must agree with it.
VERIFIERS: dict[str, Callable[[str], Any]] = {
    "weekly_workload": parse_weekly_workload,
    "education": parse_education,
    "salary": parse_money,
    "vacancies": parse_vacancies,
}


@dataclass(slots=True)
class Verification:
    field: str
    value: Any = None
    status: str = "REJECTED"  # CONFIRMED | QUOTE_NOT_FOUND | PARSER_DISAGREES | REJECTED
    quote: str = ""
    detail: str = ""

    @property
    def confirmed(self) -> bool:
        return self.status == "CONFIRMED"


def quote_is_real(document_text: str, quote: str) -> bool:
    """Is this sentence literally in the document?

    Compared on normalized text so that line wrapping, double spaces and accents in a
    PDF do not make a genuine quote look invented.
    """
    if not quote or len(quote) < MIN_QUOTE_CHARS:
        return False
    needle = normalize(quote[:MAX_QUOTE_CHARS])
    return bool(needle) and needle in normalize(document_text)


def _agrees(field: str, proposed: Any, derived: Any) -> bool:
    if derived is None:
        return False
    if proposed is None:
        return True  # the parser found it; the model only pointed at the sentence
    if field in ("weekly_workload", "vacancies"):
        try:
            return abs(float(proposed) - float(derived)) < 0.01
        except (TypeError, ValueError):
            return False
    if field == "salary":
        try:
            return abs(Decimal(str(proposed)) - Decimal(str(derived))) < Decimal("0.05")
        except (TypeError, ArithmeticError):
            return False
    return normalize(str(proposed)) == normalize(str(derived))


def verify(field: str, proposed: Any, quote: str, document_text: str) -> Verification:
    """Accept a model's claim only if the document and the parser both back it up."""
    verifier = VERIFIERS.get(field)
    if verifier is None:
        return Verification(field, status="REJECTED", detail="Campo nao verificavel")
    if not quote_is_real(document_text, quote):
        # Either invented, or too short to prove anything. Both are unusable.
        return Verification(
            field,
            status="QUOTE_NOT_FOUND",
            quote=quote[:200],
            detail="Trecho nao encontrado literalmente no documento",
        )
    derived = verifier(quote)
    if not _agrees(field, proposed, derived):
        return Verification(
            field,
            status="PARSER_DISAGREES",
            quote=quote[:200],
            detail=f"Parser leu {derived!r}, modelo afirmou {proposed!r}",
        )
    return Verification(field, value=derived, status="CONFIRMED", quote=quote[:400])


def apply_to_position(
    position: Any,
    claims: dict[str, dict[str, Any]],
    document_text: str,
    source_url: str,
    prompt_version: str,
) -> list[Verification]:
    """Fill only the gaps, and only with values the document itself proves."""
    from sentinela.domain import Evidence

    results: list[Verification] = []
    for field, claim in claims.items():
        if field not in VERIFIERS:
            continue
        if getattr(position, field, None) is not None:
            continue  # deterministic extraction already answered this
        outcome = verify(
            field, claim.get("value"), str(claim.get("raw_evidence") or ""), document_text
        )
        results.append(outcome)
        if not outcome.confirmed:
            continue
        setattr(position, field, outcome.value)
        position.evidence[field] = Evidence(
            value=outcome.value,
            status="FOUND",
            source_url=source_url,
            extraction_method=f"llm_located+parser_verified:{prompt_version}",
            # Below a table parse on purpose: the sentence was found by a model, even
            # though the number itself was read by deterministic code.
            confidence=0.85,
            raw_evidence=outcome.quote,
        )
    return results


WINDOW_BEFORE = 600
WINDOW_AFTER = 2_400
MAX_EXCERPT_CHARS = 22_000


def focus_excerpt(document_text: str, names: list[str], budget: int = MAX_EXCERPT_CHARS) -> str:
    """The regions of a long document where these cargos are actually named.

    Sending the first N characters of a 56-page edital is worse than useless: in the real
    Camara de Rio Branco edital the cargo table starts around character 146.000, so a
    head-truncated prompt would never contain the answer while still costing tokens.
    """
    from sentinela.domain import normalize_indexed

    plain, origins = normalize_indexed(document_text)
    spans: list[tuple[int, int]] = []
    for name in names:
        needle = normalize(name)
        if len(needle) < 4:
            continue
        start = 0
        while len(spans) < 40:
            found = plain.find(needle, start)
            if found < 0:
                break
            origin = origins[found]
            spans.append((max(0, origin - WINDOW_BEFORE), origin + WINDOW_AFTER))
            start = found + len(needle)
    if not spans:
        return document_text[:budget]

    spans.sort()
    merged: list[list[int]] = [list(spans[0])]
    for begin, end in spans[1:]:
        if begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])

    pieces: list[str] = []
    used = 0
    for begin, end in merged:
        chunk = document_text[begin:end]
        if used + len(chunk) > budget:
            chunk = chunk[: budget - used]
        if not chunk:
            break
        pieces.append(chunk)
        used += len(chunk)
        if used >= budget:
            break
    return ("\n[...]\n").join(pieces)


def summarize(results: list[Verification]) -> dict[str, int]:
    counts = {"confirmed": 0, "quote_not_found": 0, "parser_disagrees": 0, "rejected": 0}
    for item in results:
        counts[item.status.lower()] = counts.get(item.status.lower(), 0) + 1
    return counts
