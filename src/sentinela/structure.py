"""Build an OpportunityDraft from an extracted document.

Deterministic first: schedule tables, position tables, then labelled regex over the body.
Anything that cannot be read confidently stays None and is reported as NOT_FOUND, which is
what stops a half-read edital from turning into a confident-looking alert.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from sentinela.domain import (
    Evidence,
    FieldStatus,
    OpportunityDraft,
    PositionDraft,
    normalize,
    normalize_indexed,
)
from sentinela.eligibility import classify_qualification
from sentinela.extract import ExtractedDocument
from sentinela.parse import (
    classify_document,
    institution_implies_city,
    is_job_selection,
    location_context,
    parse_benefits,
    parse_date,
    parse_dates,
    parse_edital_number,
    parse_employment_regime,
    parse_employment_type,
    parse_fee_exemption,
    parse_money,
    parse_period,
    parse_publication_date,
    parse_vacancies,
    parse_weekly_workload,
)
from sentinela.tables import RowFacts, Table, html_tables, pdf_tables, read_tables

MAX_POSITIONS = 120

# Schedule rows are matched in order; the first marker that fits a row claims it.
SCHEDULE_MARKERS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("fee_exemption_deadline", ("isencao", "isento"), False),
    (
        "payment_deadline",
        ("pagamento da taxa", "geracao e pagamento", "pagamento do boleto"),
        False,
    ),
    (
        "registration",
        ("periodo de inscricoes", "periodo das inscricoes", "inscricoes", "prazo de inscricao"),
        True,
    ),
    (
        "exam_date",
        (
            "realizacao das provas",
            "aplicacao das provas",
            "realizacao da prova",
            "provas objetivas",
        ),
        False,
    ),
    ("publication_date", ("publicacao do edital de abertura", "publicacao do edital"), False),
)
_FEE_MARKERS = ("taxa de inscricao", "valor da inscricao", "valor da taxa")
_BOARD_MARKERS = (
    "responsabilidade tecnica e operacional",
    "banca examinadora",
    "empresa organizadora",
    "instituicao organizadora",
    "executora do certame",
    "sera realizado pelo",
)
_VALIDITY = re.compile(
    r"validade\s+(?:deste|do)\s+(?:concurso|certame|processo)[^.]{0,160}?"
    r"(\d{1,2}|um|dois|tres|quatro)\s*\(?[a-zç]*\)?\s*(ano|anos|mes|meses)",
    re.IGNORECASE,
)
_STAGE_MARKERS = (
    ("Prova objetiva", ("prova objetiva", "provas objetivas")),
    ("Prova discursiva", ("prova discursiva", "prova de redacao")),
    ("Prova prática", ("prova pratica",)),
    ("Prova de títulos", ("prova de titulos", "avaliacao de titulos")),
    ("Teste de aptidão física", ("teste de aptidao fisica", "teste fisico")),
    ("Avaliação psicológica", ("avaliacao psicologica", "exame psicotecnico")),
    ("Investigação social", ("investigacao social",)),
    ("Curso de formação", ("curso de formacao",)),
    ("Análise curricular", ("analise curricular", "analise de curriculo")),
    ("Entrevista", ("entrevista",)),
)


def _evidence(
    value: Any,
    url: str,
    *,
    method: str,
    confidence: float,
    raw: str = "",
    page: int | None = None,
    status: FieldStatus = "FOUND",
) -> Evidence:
    return Evidence(
        value=value,
        status=status if value is not None else "NOT_FOUND",
        source_url=url,
        page_number=page,
        extraction_method=method,
        confidence=confidence if value is not None else 0,
        raw_evidence=" ".join(str(raw).split())[:400],
    )


def schedule_from_tables(tables: list[Table]) -> dict[str, tuple[date, str, int | None]]:
    """Read Anexo-V style schedule tables: description plus start and end dates."""
    found: dict[str, tuple[date, str, int | None]] = {}
    for table in tables:
        header = " ".join(normalize(cell) for cell in (table.rows[0] if table.rows else []))
        if not (
            "descricao" in header
            or "atividade" in header
            or "cronograma" in header
            or "evento" in header
        ):
            continue
        for row in table.rows[1:]:
            description = _describing_cell(row)
            plain = normalize(description)
            if not plain:
                continue
            dates: list[date] = []
            for cell in row:
                if cell == description:
                    continue
                dates.extend(value for value in parse_dates(cell) if value not in dates)
            dates = dates or parse_dates(description)
            if not dates:
                continue
            for field, markers, is_period in SCHEDULE_MARKERS:
                if not any(marker in plain for marker in markers):
                    continue
                if is_period:
                    if "registration_start" not in found:
                        found["registration_start"] = (min(dates), description, table.page)
                    if "registration_deadline" not in found:
                        found["registration_deadline"] = (max(dates), description, table.page)
                elif field not in found:
                    found[field] = (max(dates), description, table.page)
                break
    return found


def _describing_cell(row: list[str]) -> str:
    """The cell that names the activity: the one with the most alphabetic words."""

    def words(cell: str) -> int:
        plain = normalize(cell)
        return sum(1 for word in plain.split() if len(word) > 2 and not word.isdigit())

    best = max(row, key=words, default="")
    return best if words(best) >= 2 else ""


def _locate(text: str, markers: tuple[str, ...]) -> int:
    """Index in `text` of the first marker, matched accent- and case-insensitively."""
    plain, origins = normalize_indexed(text)
    for marker in markers:
        index = plain.find(marker)
        if index >= 0:
            return origins[index]
    return -1


def _labelled(text: str, markers: tuple[str, ...], window: int = 320) -> str:
    """Raw text starting at the first occurrence of any marker, for regex extraction."""
    index = _locate(text, markers)
    return "" if index < 0 else text[index : index + window]


def fee_from_tables(
    tables: list[Table], education: str = "high_school"
) -> tuple[Decimal | None, str]:
    """Application fee, preferring the row that matches the education level we care about."""
    wanted = {"high_school": "medio", "higher_education": "superior", "primary": "fundamental"}
    marker = wanted.get(education, "medio")
    candidates: list[tuple[Decimal, str, bool]] = []
    for table in tables:
        header = " ".join(normalize(cell) for row in table.rows[:2] for cell in row)
        if not any(word in header for word in _FEE_MARKERS):
            continue
        for row in table.rows[1:]:
            line = " ".join(row)
            value = parse_money(line)
            if value is not None and value > 0:
                candidates.append((value, " ".join(line.split())[:200], marker in normalize(line)))
    if not candidates:
        return None, ""
    for value, raw, matched in candidates:
        if matched:
            return value, raw
    cheapest = min(candidates, key=lambda entry: entry[0])
    return cheapest[0], cheapest[1]


_NAME_HINTS = (
    "concurso publico",
    "processo seletivo",
    "selecao publica",
    "chamada publica",
    "edital de abertura",
)


_NAME_PHRASE = re.compile(
    r"((?:concurso\s+p[úu]blico|processo\s+seletivo(?:\s+simplificado)?|sele[çc][ãa]o\s+p[úu]blica"
    r"|chamada\s+p[úu]blica)[^.;\n]{0,120})",
    re.IGNORECASE,
)


def concurso_name(title: str, text: str, institution: str, edital_number: str | None) -> str:
    """A name a human recognises.

    PDF text wraps mid-phrase, so the header region is collapsed onto one line before the
    concurso phrase is matched; matching line by line truncates at the wrap.
    """
    header = " ".join(f"{title}\n{text[:2500]}".split())
    if match := _NAME_PHRASE.search(header):
        candidate = " ".join(match[1].split()).strip(" ,-–—")
        # Cut a trailing "EDITAL Nº .." that the phrase ran into.
        candidate = re.split(r"\bEDITAL\b", candidate, maxsplit=1, flags=re.IGNORECASE)[0]
        candidate = candidate.strip(" ,-–—")
        if len(candidate) >= 12:
            suffix = f" — Edital {edital_number}" if edital_number else ""
            return f"{candidate}{suffix}"[:250]
    suffix = f" — Edital {edital_number}" if edital_number else ""
    head = " ".join((title or "").split())[:120]
    return f"{head or institution}{suffix}"[:250]


def _schedule_from_text(text: str) -> dict[str, tuple[date, str, int | None]]:
    found: dict[str, tuple[date, str, int | None]] = {}
    window = _labelled(
        text,
        (
            "periodo de inscricoes",
            "periodo das inscricoes",
            "inscricoes serao realizadas",
            "das inscricoes",
        ),
        420,
    )
    if window:
        start, end = parse_period(window)
        if start:
            found["registration_start"] = (start, window, None)
        if end:
            found["registration_deadline"] = (end, window, None)
    exam = _labelled(text, ("realizacao das provas", "aplicacao das provas", "data da prova"), 320)
    if exam and (value := parse_date(exam)):
        found["exam_date"] = (value, exam, None)
    return found


def _stages(text: str) -> list[str]:
    plain = normalize(text)
    return [label for label, markers in _STAGE_MARKERS if any(m in plain for m in markers)]


def _validity(text: str) -> str | None:
    if match := _VALIDITY.search(text or ""):
        words = {"um": "1", "dois": "2", "tres": "3", "quatro": "4"}
        amount = words.get(match[1].lower(), match[1])
        unit = "ano(s)" if match[2].lower().startswith("ano") else "mes(es)"
        return f"{amount} {unit}"
    return None


_BOARD_NOISE = (
    "EDITAL",
    "ANEXO",
    "CONCURSO",
    "CAMARA",
    "CÂMARA",
    "PREFEITURA",
    "ESTADO",
    "MUNICIPIO",
    "MUNICÍPIO",
    "CAPITULO",
    "CAPÍTULO",
    "SECAO",
    "SEÇÃO",
    "DAS ",
    "DOS ",
    "DA ",
    "DO ",
    "PROCESSO",
    "PODER",
)


def _board(text: str, fallback: str | None) -> str | None:
    """Organizing boards are written in caps right after the responsibility clause."""
    # Collapse wrapping: the board name routinely breaks across PDF lines.
    window = " ".join(_labelled(text, _BOARD_MARKERS, 320).split())
    for match in re.finditer(r"\b([A-ZÀ-Ü][A-ZÀ-Ü&.\- ]{4,80})\b", window):
        name = " ".join(match[1].split()).strip(" -.")
        if len(name) < 4 or any(name.startswith(noise) for noise in _BOARD_NOISE):
            continue
        # "INSTITUTO ... BRASILEIRO - IDIB": the trailing acronym is the usable identifier.
        if acronym := re.match(
            r"\s*[-–—]\s*([A-Z]{2,10})\b", window[match.end() : match.end() + 24]
        ):
            return acronym[1]
        return name[:120]
    return fallback


def _merge_rows(rows: list[RowFacts]) -> list[RowFacts]:
    """One entry per cargo, combining the vacancy table with the requirements table."""
    merged: dict[str, RowFacts] = {}
    for row in rows:
        key = normalize(row.name)
        if not key:
            continue
        current = merged.get(key)
        if current is None:
            merged[key] = row
            continue
        for field in ("requirements", "location"):
            if not getattr(current, field) and getattr(row, field):
                setattr(current, field, getattr(row, field))
        for field in (
            "salary",
            "weekly_workload",
            "daily_workload",
            "vacancies",
            "reserve_count",
            "education",
        ):
            if getattr(current, field) is None and getattr(row, field) is not None:
                setattr(current, field, getattr(row, field))
        current.raw = current.raw + row.raw
    return list(merged.values())[:MAX_POSITIONS]


def _position_from_row(
    row: RowFacts,
    document: ExtractedDocument,
    url: str,
    city: str,
    doc_assignment: bool,
    doc_locations: list[str],
    doc_snippet: str,
    doc_workload: float | None,
    institutional: bool = False,
    benefits: str | None = None,
) -> PositionDraft:
    category, education, extras = classify_qualification(row.requirements)
    if education is None:
        education = row.education
    if education == "high_school" and category == "UNKNOWN":
        # The vacancy table said "Cargos de Nivel Medio" but no requirement text was found.
        category = "HIGH SCHOOL ONLY"
    workload = row.weekly_workload
    workload_method = "table_parser"
    if workload is None and doc_workload is not None:
        workload, workload_method = doc_workload, "document_regex"
    local_text = row.location or doc_snippet
    assignment = bool(row.location) or doc_assignment
    locations = [row.location] if row.location else list(doc_locations)
    evidence = {
        "education": _evidence(
            education,
            url,
            method="table_parser" if row.requirements else "section_header",
            confidence=0.95 if row.requirements else 0.8,
            raw=row.requirements or " | ".join(row.raw)[:200],
        ),
        "requirements": _evidence(
            row.requirements or None,
            url,
            method="table_parser",
            confidence=0.95,
            raw=row.requirements,
        ),
        "salary": _evidence(
            row.salary, url, method="table_parser", confidence=0.97, raw=" | ".join(row.raw)[:200]
        ),
        "weekly_workload": _evidence(
            workload,
            url,
            method=workload_method,
            confidence=0.95 if workload_method == "table_parser" else 0.6,
            raw=" | ".join(row.raw)[:200],
        ),
        "vacancies": _evidence(
            row.vacancies, url, method="table_parser", confidence=0.9, raw=" | ".join(row.raw)[:200]
        ),
        "assignment_location": _evidence(
            city if assignment else None,
            url,
            method=(
                "table_parser"
                if row.location
                else "institution_scope"
                if institutional
                else "document_context"
            ),
            confidence=(
                0.95 if row.location else 0.92 if institutional else 0.75 if doc_assignment else 0.0
            ),
            raw=local_text,
        ),
    }
    return PositionDraft(
        name=" ".join(row.name.split())[:200],
        education=education,
        additional_qualifications=extras,
        qualification_category=category,
        vacancies=row.vacancies,
        reserve_list=bool(row.reserve_count),
        reserve_count=row.reserve_count,
        assignment_location=city if assignment else None,
        possible_assignment_locations=[loc for loc in locations if loc],
        assignment_confirmed=assignment,
        salary=row.salary,
        weekly_workload=workload,
        daily_workload=row.daily_workload,
        benefits=benefits,
        requirements=row.requirements or None,
        evidence=evidence,
    )


def build_opportunity(
    document: ExtractedDocument,
    content: bytes,
    url: str,
    *,
    institution: str,
    city: str,
    media_type: str,
    source_board: str | None = None,
    header_date: str | None = None,
) -> OpportunityDraft:
    text = document.text
    tables = pdf_tables(content) if media_type == "pdf" else html_tables(content)
    rows = _merge_rows(read_tables(tables, city))
    title = document.title or text.split("\n", 1)[0][:200]

    assignment, exam_only, snippet = location_context(text, city)
    institutional = institution_implies_city(institution, city)
    if institutional and not assignment:
        assignment, exam_only = True, False
        snippet = (
            snippet or f"Órgão municipal de {city}: lotação decorre da própria natureza do órgão"
        )
    doc_locations = [city] if assignment else []
    doc_workload = parse_weekly_workload(text)
    benefits = parse_benefits(text)
    schedule = schedule_from_tables(tables) or {}
    for key, value in _schedule_from_text(text).items():
        schedule.setdefault(key, value)

    publication = None
    if entry := schedule.get("publication_date"):
        publication = entry[0]
    publication = publication or parse_publication_date(title, text, header_date)
    document_type = classify_document(title, text)
    edital_number = parse_edital_number(title) or parse_edital_number(text[:6000])
    fee, fee_window = fee_from_tables(tables)
    if fee is None:
        fee_window = _labelled(text, _FEE_MARKERS, 260)
        fee = parse_money(fee_window)

    def scheduled(field: str, method: str = "schedule_table") -> Evidence:
        entry = schedule.get(field)
        if not entry:
            return _evidence(None, url, method=method, confidence=0)
        return _evidence(entry[0], url, method=method, confidence=0.96, raw=entry[1], page=entry[2])

    draft = OpportunityDraft(
        institution=institution,
        name=concurso_name(title, text, institution, edital_number),
        edital_number=edital_number,
        publication_date=publication,
        employment_type=parse_employment_type(f"{title}\n{text[:8000]}"),
        employment_regime=parse_employment_regime(text),
        fee_exemption=parse_fee_exemption(text),
        organizing_board=_board(text, source_board),
        registration_start=(schedule.get("registration_start") or (None,))[0],
        registration_deadline=(schedule.get("registration_deadline") or (None,))[0],
        application_fee=fee,
        fee_exemption_deadline=(schedule.get("fee_exemption_deadline") or (None,))[0],
        payment_deadline=(schedule.get("payment_deadline") or (None,))[0],
        exam_date=(schedule.get("exam_date") or (None,))[0],
        exam_location=city if exam_only or assignment else None,
        selection_stages=_stages(text),
        validity=_validity(text),
        official_edital_url=url if media_type == "pdf" else None,
        official_institution_url=url,
        status="EDITAL_PUBLISHED",
        document_type=document_type,
    )
    draft.evidence = {
        "publication_date": _evidence(
            publication, url, method="labelled_regex", confidence=0.9, raw=title
        ),
        "registration_start": scheduled("registration_start"),
        "registration_deadline": scheduled("registration_deadline"),
        "exam_date": scheduled("exam_date"),
        "payment_deadline": scheduled("payment_deadline"),
        "fee_exemption_deadline": scheduled("fee_exemption_deadline"),
        "application_fee": _evidence(
            fee, url, method="labelled_regex", confidence=0.85, raw=fee_window
        ),
        "edital_number": _evidence(edital_number, url, method="regex", confidence=0.95, raw=title),
        "employment_type": _evidence(
            draft.employment_type, url, method="keyword", confidence=0.85, raw=title
        ),
    }

    if rows:
        draft.positions = [
            _position_from_row(
                row,
                document,
                url,
                city,
                assignment,
                doc_locations,
                snippet,
                doc_workload,
                institutional,
                benefits,
            )
            for row in rows
        ]
    else:
        # No position table: keep one aggregate candidate alive for the review queue rather
        # than discarding a document that may well describe a suitable opportunity.
        category, education, extras = classify_qualification(text[:20000])
        draft.review_reasons.append("Nenhuma tabela de cargos reconhecida no documento")
        draft.positions = [
            PositionDraft(
                name=draft.name[:200],
                education=education,
                qualification_category=category,
                additional_qualifications=extras,
                vacancies=parse_vacancies(text[:20000]),
                assignment_location=city if assignment else None,
                possible_assignment_locations=doc_locations,
                assignment_confirmed=assignment,
                salary=None,
                weekly_workload=doc_workload,
                requirements=None,
                synthetic=True,
                evidence={
                    "education": _evidence(
                        education, url, method="document_regex", confidence=0.55, raw=text[:200]
                    ),
                    "weekly_workload": _evidence(
                        doc_workload, url, method="document_regex", confidence=0.55, raw=text[:200]
                    ),
                    "assignment_location": _evidence(
                        city if assignment else None,
                        url,
                        method="document_context",
                        confidence=0.7 if assignment else 0.0,
                        raw=snippet,
                    ),
                },
            )
        ]
    has_signal = (
        bool(rows) or bool(edital_number) or bool(draft.registration_deadline or draft.exam_date)
    )
    draft.material = has_signal and is_job_selection(title, text)
    if has_signal and not draft.material:
        draft.review_reasons.append(
            "Documento usa vocabulário de certame mas não oferece cargo público"
        )
    if exam_only and not assignment:
        draft.review_reasons.append(
            f"{city} aparece apenas como local de prova; lotação não confirmada"
        )
    if document.truncated:
        draft.review_reasons.append("Texto truncado no limite de tamanho")
    if not publication:
        draft.review_reasons.append("Data de publicação não localizada")
    return draft
