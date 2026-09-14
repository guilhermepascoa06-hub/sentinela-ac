"""Table extraction and cell classification.

Editais put the facts that matter -- cargo, requisito, remuneracao, carga horaria, vagas --
in tables. Column headers are unreliable (they wrap across rows and shift by a column), so
cells are classified by their own content rather than by the header above them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sentinela.domain import normalize
from sentinela.parse import (
    parse_daily_workload,
    parse_education,
    parse_money,
    parse_weekly_workload,
)

MAX_TABLES = 60
MAX_ROWS = 400
_INTEGER = re.compile(r"^\s*0*(\d{1,4})\s*$")
_DASH = re.compile(r"^\s*[-–—]?\s*$")
_LEVEL_ROW = re.compile(r"cargos? de nivel (medio|superior|fundamental)")
_TOTAL_ROW = re.compile(r"^(totais?|total geral|subtotal)\b")
# A cargo name: letters, reasonable length, not a sentence.
_NAME_STOPWORDS = (
    "cargo",
    "especialidade",
    "requisito",
    "remuneracao",
    "carga",
    "horaria",
    "atribuicoes",
    "vagas",
    "cadastro de reserva",
    "ampla concorrencia",
    "reservadas",
    "escolaridade",
    "nivel",
    "total",
    "lotacao",
    "municipio",
    "codigo",
    "quantidade",
    "salario",
    "anexo",
)


@dataclass(slots=True)
class Table:
    rows: list[list[str]]
    page: int | None = None
    caption: str = ""


@dataclass(slots=True)
class RowFacts:
    """What a single table row states, with the raw cells kept for evidence."""

    name: str = ""
    requirements: str = ""
    education: str | None = None
    salary: Decimal | None = None
    weekly_workload: float | None = None
    daily_workload: float | None = None
    vacancies: int | None = None
    reserve_count: int | None = None
    location: str = ""
    raw: list[str] = field(default_factory=list)

    @property
    def informative(self) -> bool:
        return bool(self.name) and any(
            (self.requirements, self.salary, self.weekly_workload, self.vacancies is not None)
        )


def clean(cell: Any) -> str:
    return " ".join(str(cell or "").split())


def tidy_rows(rows: list[list[Any]]) -> list[list[str]]:
    """Drop all-empty columns and rows so cell positions stay meaningful."""
    cleaned = [[clean(cell) for cell in row] for row in rows[:MAX_ROWS]]
    if not cleaned:
        return []
    width = max(len(row) for row in cleaned)
    padded = [row + [""] * (width - len(row)) for row in cleaned]
    keep = [index for index in range(width) if any(row[index] for row in padded)]
    trimmed = [[row[index] for index in keep] for row in padded]
    return [row for row in trimmed if any(row)]


def pdf_tables(content: bytes, max_pages: int = 180) -> list[Table]:
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        return []
    found: list[Table] = []
    try:
        with pymupdf.open(stream=content, filetype="pdf") as document:
            for number, page in enumerate(list(document)[:max_pages], start=1):
                if len(found) >= MAX_TABLES:
                    break
                try:
                    finder = page.find_tables()
                except Exception:  # noqa: BLE001 - table finding is best effort
                    continue
                for table in finder.tables:
                    rows = tidy_rows(table.extract())
                    if len(rows) >= 2:
                        found.append(Table(rows=rows, page=number))
    except Exception:  # noqa: BLE001 - a corrupt PDF yields no tables, not a crash
        return found
    return found


def html_tables(content: bytes) -> list[Table]:
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(content, "html.parser")
    except Exception:  # noqa: BLE001
        return []
    found: list[Table] = []
    for element in soup.find_all("table")[:MAX_TABLES]:
        rows = tidy_rows(
            [
                [cell.get_text(" ", strip=True) for cell in line.find_all(["td", "th"])]
                for line in element.find_all("tr")
            ]
        )
        if len(rows) >= 2:
            caption = element.find("caption")
            found.append(
                Table(rows=rows, caption=caption.get_text(" ", strip=True) if caption else "")
            )
    return found


def looks_like_name(cell: str) -> bool:
    plain = normalize(cell)
    if not (4 <= len(plain) <= 140):
        return False
    if _INTEGER.match(cell) or _DASH.match(cell):
        return False
    if not re.search(r"[a-zA-Z]{3}", cell):
        return False
    if any(plain.startswith(word) or plain == word for word in _NAME_STOPWORDS):
        return False
    if _TOTAL_ROW.match(plain) or _LEVEL_ROW.search(plain):
        return False
    # Sentences and attribution paragraphs are not cargo names.
    return plain.count(" ") <= 14 and cell.count(";") == 0 and cell.count(".") <= 2


def classify_row(cells: list[str], level_hint: str | None = None, city: str = "") -> RowFacts:
    facts = RowFacts(raw=list(cells))
    integers: list[int] = []
    for cell in cells:
        if not cell:
            continue
        plain = normalize(cell)
        if match := _INTEGER.match(cell):
            integers.append(int(match[1]))
            continue
        # normalize() strips "$", so currency must be detected on the raw cell.
        if facts.salary is None and "r$" in cell.lower().replace(" ", ""):
            facts.salary = parse_money(cell)
            if facts.salary is not None:
                continue
        if facts.weekly_workload is None and ("hora" in plain or re.search(r"\d+\s*h\b", plain)):
            facts.weekly_workload = parse_weekly_workload(cell)
            facts.daily_workload = facts.daily_workload or parse_daily_workload(cell)
            if facts.weekly_workload is not None:
                continue
        if len(cell) > 40 and any(
            marker in plain
            for marker in (
                "diploma",
                "certificado",
                "conclusao de curso",
                "ensino",
                "nivel",
                "registro no",
                "habilitacao",
                "curso tecnico",
                "experiencia",
            )
        ):
            facts.requirements = f"{facts.requirements} {cell}".strip()
            continue
        if city and normalize(city) in plain and len(plain) <= 90:
            facts.location = cell
            continue
        if not facts.name and looks_like_name(cell):
            facts.name = cell
    facts.education = parse_education(facts.requirements) or level_hint
    if integers:
        # First integer in a vacancy row is the total for the cargo; a trailing one is the
        # reserve list. Anything larger than 5000 is a law number that slipped through.
        usable = [value for value in integers if value <= 5000]
        if usable:
            facts.vacancies = usable[0]
            if len(usable) > 2:
                facts.reserve_count = usable[-1]
    return facts


def level_from_row(cells: list[str]) -> str | None:
    for cell in cells:
        if match := _LEVEL_ROW.search(normalize(cell)):
            return {
                "medio": "high_school",
                "superior": "higher_education",
                "fundamental": "primary",
            }[match[1]]
    return None


# A position table names cargos AND states something about the job. Tables that merely
# mention cargos (exam duration, fee per cargo, discursive-test cut-offs) are excluded --
# they are the main source of false positions in real editais.
_CARGO_HEADER = ("cargo", "especialidade", "funcao", "emprego")
_JOB_HEADER = (
    "vaga",
    "requisito",
    "remuneracao",
    "carga hor",
    "escolaridade",
    "lotacao",
    "vencimento",
    "salario",
    "cadastro de reserva",
    "nivel de escolaridade",
    "atribuicoes",
    "jornada",
)


def is_position_table(table: Table) -> bool:
    header = " | ".join(normalize(cell) for row in table.rows[:3] for cell in row)
    if not any(word in header for word in _CARGO_HEADER):
        return False
    return any(word in header for word in _JOB_HEADER)


def read_tables(tables: list[Table], city: str = "") -> list[RowFacts]:
    """Every informative row across every position table, with section level context."""
    facts: list[RowFacts] = []
    for table in (candidate for candidate in tables if is_position_table(candidate)):
        level: str | None = None
        for row in table.rows:
            if found := level_from_row(row):
                level = found
                continue
            if _TOTAL_ROW.match(normalize(row[0] if row else "")):
                continue
            entry = classify_row(row, level, city)
            if entry.informative:
                facts.append(entry)
    return facts
