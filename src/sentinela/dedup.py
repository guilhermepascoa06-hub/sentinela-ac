"""Deduplication.

The same certame reaches us from the Diario Oficial, the institution, the board and an
aggregator. It must become one opportunity with several pieces of evidence.

Identity is decided by explicit keys first (institution + edital number is effectively a
primary key in Brazilian public administration) and only falls back to similarity when no
explicit key exists. Fuzzy matching is never the sole criterion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sentinela.domain import OpportunityDraft, normalize

# Words that carry no distinguishing power when comparing two concurso names.
_STOPWORDS = frozenset(
    """de da do das dos e para com no na nos nas o a os as um uma em por edital concurso
    publico publica processo seletivo simplificado abertura vagas cargo cargos nivel
    provimento municipio estado prefeitura camara secretaria""".split()
)
_INSTITUTION_NOISE = frozenset(
    """prefeitura municipal camara municipio governo estado secretaria de do da dos das e
    instituto tribunal conselho regional federal fundacao universidade empresa companhia
    banco publica publico""".split()
)


def institution_key(institution: str) -> str:
    """Stable short key that still separates two bodies in the same city.

    Stripping the organisation-type word is what makes "Camara Municipal de Rio Branco"
    and "Prefeitura Municipal de Rio Branco" collapse into the same key, so the leading
    word is always kept as the discriminator.
    """
    plain = normalize(institution)
    words = plain.split()
    if not words:
        return "desconhecida"
    distinctive = [word for word in words if word not in _INSTITUTION_NOISE]
    head = words[0]
    parts = [head] + [word for word in distinctive if word != head]
    return "-".join(parts[:4])[:60]


def tokens(value: str) -> frozenset[str]:
    return frozenset(
        word for word in normalize(value).split() if word not in _STOPWORDS and len(word) > 2
    )


def similarity(left: str, right: str) -> float:
    """Jaccard over meaningful tokens. Deterministic, explainable, no model involved."""
    a, b = tokens(left), tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedup_key(draft: OpportunityDraft) -> str:
    """The database identity of an opportunity.

    With an edital number this is exact. Without one it degrades to institution + year +
    the strongest name tokens, which is why `find_match` still runs a similarity pass.
    """
    base = institution_key(draft.institution)
    if draft.edital_number:
        return f"{base}:edital:{normalize(draft.edital_number).replace(' ', '/')}"[:128]
    year = (draft.publication_date or date(1900, 1, 1)).year
    signature = "-".join(sorted(tokens(draft.name))[:6]) or "sem-nome"
    return f"{base}:{year}:{signature}"[:128]


@dataclass(slots=True)
class Candidate:
    """Minimal view of a stored opportunity, so dedup does not depend on the ORM."""

    id: str
    dedup_key: str
    institution: str
    name: str
    edital_number: str | None
    publication_date: date | None
    organizing_board: str | None
    urls: tuple[str, ...] = ()
    document_hashes: tuple[str, ...] = ()


def find_match(
    draft: OpportunityDraft,
    candidates: list[Candidate],
    *,
    url: str = "",
    document_hash: str = "",
    threshold: float = 0.62,
) -> tuple[Candidate | None, str]:
    """Return the stored opportunity this draft belongs to, and why it matched."""
    key = dedup_key(draft)
    for candidate in candidates:
        if candidate.dedup_key == key:
            return candidate, "dedup_key"
    if document_hash:
        for candidate in candidates:
            if document_hash in candidate.document_hashes:
                return candidate, "document_hash"
    if url:
        for candidate in candidates:
            if url in candidate.urls:
                return candidate, "url"
    if draft.edital_number:
        target = normalize(draft.edital_number)
        for candidate in candidates:
            if (
                candidate.edital_number
                and normalize(candidate.edital_number) == target
                and similarity(candidate.institution, draft.institution) >= 0.34
            ):
                return candidate, "edital_number"
    # Similarity is the last resort and still needs the institution and the year to agree.
    best: tuple[float, Candidate | None] = (0.0, None)
    for candidate in candidates:
        if similarity(candidate.institution, draft.institution) < 0.5:
            continue
        if (
            draft.publication_date
            and candidate.publication_date
            and draft.publication_date.year != candidate.publication_date.year
        ):
            continue
        score = similarity(candidate.name, draft.name)
        if draft.organizing_board and candidate.organizing_board:
            if normalize(draft.organizing_board) == normalize(candidate.organizing_board):
                score += 0.08
        if score > best[0]:
            best = (score, candidate)
    if best[1] is not None and best[0] >= threshold:
        return best[1], f"similarity:{best[0]:.2f}"
    return None, "new"


def position_slug(name: str) -> str:
    """Stable identity for a cargo inside one opportunity."""
    plain = normalize(name)
    return re.sub(r"\s+", "-", plain)[:160] or "cargo"
