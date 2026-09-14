import hashlib
import json
import re
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PARSER_VERSION = "2026.09.14.2"  # regime juridico, beneficios e condicoes de isencao
FieldStatus = Literal["FOUND", "NOT_FOUND", "AMBIGUOUS", "CONFLICTING"]


def now() -> datetime:
    return datetime.now(UTC)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def normalize(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(
        re.sub(
            r"[^a-z0-9]+", " ", "".join(c for c in plain if not unicodedata.combining(c))
        ).split()
    )


def normalize_indexed(value: str) -> tuple[str, list[int]]:
    """normalize() plus, for each output character, the index it came from in `value`.

    Searching the normalized text and mapping the hit back to the original is the only way
    to locate an accent-insensitive marker precisely; estimating the offset by length ratio
    lands in the wrong paragraph on long documents.
    """
    characters: list[str] = []
    origins: list[int] = []
    for index, character in enumerate(value):
        decomposed = unicodedata.normalize("NFKD", character.casefold())
        for piece in decomposed:
            if unicodedata.combining(piece):
                continue
            characters.append(piece if piece.isalnum() else " ")
            origins.append(index)
    out: list[str] = []
    out_origins: list[int] = []
    previous_space = True
    for character, origin in zip(characters, origins, strict=True):
        if character == " ":
            if previous_space:
                continue
            previous_space = True
        else:
            if not ("a" <= character <= "z" or character.isdigit()):
                character = " "
                if previous_space:
                    continue
                previous_space = True
            else:
                previous_space = False
        out.append(character)
        out_origins.append(origin)
    while out and out[-1] == " ":
        out.pop()
        out_origins.pop()
    return "".join(out), out_origins


def digest(value: Any) -> str:
    raw = (
        value
        if isinstance(value, bytes)
        else json.dumps(
            value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
        ).encode()
    )
    return hashlib.sha256(raw).hexdigest()


class Evidence(BaseModel):
    value: Any = None
    status: FieldStatus = "NOT_FOUND"
    source_url: str
    document_id: str | None = None
    document_version_id: str | None = None
    page_number: int | None = None
    extraction_method: str = "deterministic"
    confidence: float = Field(default=0, ge=0, le=1)
    raw_evidence: str = ""


class PositionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    education: str | None = None
    additional_qualifications: list[str] = []
    qualification_category: str = "UNKNOWN"
    vacancies: int | None = Field(default=None, ge=0)
    reserve_list: bool | None = None
    reserve_count: int | None = Field(default=None, ge=0)
    assignment_location: str | None = None
    possible_assignment_locations: list[str] = []
    assignment_confirmed: bool = False
    salary: Decimal | None = Field(default=None, ge=0)
    benefits: str | None = None
    weekly_workload: float | None = Field(default=None, gt=0, le=168)
    daily_workload: float | None = Field(default=None, gt=0, le=24)
    requirements: str | None = None
    # True when no cargo table was found and this stands in for the whole document. It is
    # only ever stored while the opportunity has no real cargo of its own.
    synthetic: bool = False
    evidence: dict[str, Evidence] = {}


class OpportunityDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    institution: str
    name: str
    edital_number: str | None = None
    publication_date: date | None = None
    employment_type: str = "UNKNOWN"
    employment_regime: str | None = None
    organizing_board: str | None = None
    registration_start: date | None = None
    registration_deadline: date | None = None
    registration_end_time: str | None = None
    application_fee: Decimal | None = None
    fee_exemption: str | None = None
    fee_exemption_deadline: date | None = None
    payment_deadline: date | None = None
    exam_date: date | None = None
    exam_location: str | None = None
    selection_stages: list[str] = []
    validity: str | None = None
    official_edital_url: str | None = None
    official_institution_url: str | None = None
    official_application_url: str | None = None
    organizing_board_url: str | None = None
    status: str = "EDITAL_PUBLISHED"
    document_type: str = "OPENING"
    evidence: dict[str, Evidence] = {}
    positions: list[PositionDraft] = []
    conflicts: list[dict[str, Any]] = []
    review_reasons: list[str] = []
    # False on a listing/navigation page that names a concurso but states no edital
    # number, no dates and no cargo table. Such a page is kept as a document for later
    # retry, but must not become an opportunity row.
    material: bool = True


class Eligibility(BaseModel):
    eligible: bool
    primary_alert: bool
    workload_classification: str
    score: int = Field(ge=0, le=100)
    rank: str
    confidence: str
    reasons: list[str]
    needs_review: bool


class SourceSpec(BaseModel):
    id: str
    name: str
    institution: str
    base_url: str
    official: bool = True
    trust_level: int = Field(default=1, ge=1, le=4)
    priority: int = 10
    adapter: str = "generic"
    discovery_method: str = "html"
    enabled: bool = True
    trust_status: str = "TRUSTED"
    allowed_hosts: list[str] = []
    seed_urls: list[str] = []
    link_pattern: str = r"concurso|seletiv|edital|retifica|prorroga|\.pdf(?:\?|$)"
    max_documents: int = 10
    max_depth: int = 2
    expected_min_links: int = 0
    # True when the listing is built by JavaScript and plain HTTP sees an empty page.
    render: bool = False
    validation_url: str = ""
    validated_at: str | datetime | None = None
    limitations: str = ""
