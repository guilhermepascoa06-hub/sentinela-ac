"""Deterministic field parsers.

Every function here either returns a confident value or returns nothing. None of them
guess: an unparseable string produces None and the caller records NOT_FOUND, which is
what keeps a missing salary out of the alert instead of inventing one.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sentinela.domain import normalize, normalize_indexed

MONTHS = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
    "jan": 1,
    "fev": 2,
    "mar": 3,
    "abr": 4,
    "mai": 5,
    "jun": 6,
    "jul": 7,
    "ago": 8,
    "set": 9,
    "out": 10,
    "nov": 11,
    "dez": 12,
}
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_TEXT_DATE = re.compile(
    r"\b(\d{1,2})\s*(?:de\s+)?([a-zç]{3,9})\.?\s*(?:de\s+|/)?\s*(\d{4})\b", re.IGNORECASE
)
# R$ 4.656,75 and R$ 4656.75 both appear in real editais.
_MONEY = re.compile(r"R\$\s*([\d][\d.\s]*(?:,\d{2})?|\d+(?:\.\d{2})?)", re.IGNORECASE)
_WEEKLY = re.compile(
    r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:\(?[a-zç\s]{0,20}\)?\s*)?"
    r"(?:h(?:oras?)?|horas)\s*(?:/|\s+)?\s*(?:semanais|semana|semanal|por\s+semana|sem\b)",
    re.IGNORECASE,
)
_DAILY = re.compile(
    r"(\d{1,2}(?:[.,]\d{1,2})?)\s*(?:h(?:oras?)?|horas)\s*(?:/|\s+)?\s*"
    r"(?:di[áa]ri[ao]s?|dia\b|por\s+dia)",
    re.IGNORECASE,
)
_VACANCIES = re.compile(r"\b(\d{1,4})\s*(?:\(.*?\)\s*)?vagas?\b", re.IGNORECASE)
_EDITAL_NUMBER = re.compile(
    r"edital\s*(?:de\s+)?(?:abertura\s*)?(?:n[ºo°.\s]*)?\s*(\d{1,4})\s*[/-]\s*(\d{4})",
    re.IGNORECASE,
)


def _year(raw: str) -> int:
    value = int(raw)
    if value < 100:
        # Two-digit years in editais are always this century in practice.
        return 2000 + value
    return value


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        result = date(year, month, day)
    except ValueError:
        return None
    # Anything outside this band is a page number or a typo, not a date.
    return result if 2000 <= result.year <= 2100 else None


def parse_date(text: str) -> date | None:
    """First plausible date in `text`, preferring the unambiguous formats."""
    if not text:
        return None
    if match := _ISO_DATE.search(text):
        return _safe_date(int(match[1]), int(match[2]), int(match[3]))
    if match := _NUMERIC_DATE.search(text):
        return _safe_date(_year(match[3]), int(match[2]), int(match[1]))
    for match in _TEXT_DATE.finditer(text):
        month = MONTHS.get(normalize(match[2]))
        if month:
            return _safe_date(int(match[3]), month, int(match[1]))
    return None


def parse_dates(text: str) -> list[date]:
    """Every distinct date in `text`, in order of appearance."""
    found: list[date] = []
    for pattern, order in ((_ISO_DATE, "ymd"), (_NUMERIC_DATE, "dmy"), (_TEXT_DATE, "text")):
        for match in pattern.finditer(text or ""):
            if order == "ymd":
                value = _safe_date(int(match[1]), int(match[2]), int(match[3]))
            elif order == "dmy":
                value = _safe_date(_year(match[3]), int(match[2]), int(match[1]))
            else:
                month = MONTHS.get(normalize(match[2]))
                value = _safe_date(int(match[3]), month, int(match[1])) if month else None
            if value and value not in found:
                found.append(value)
    return found


def parse_period(text: str) -> tuple[date | None, date | None]:
    """A registration window such as `de 15/09/2026 a 13/10/2026`."""
    dates = parse_dates(text)
    if len(dates) < 2:
        return (dates[0] if dates else None), None
    ordered = sorted(dates)
    return ordered[0], ordered[-1]


def parse_money(text: str) -> Decimal | None:
    """Brazilian currency. `1.500` is one thousand five hundred reais, never R$ 1,50."""
    if not text:
        return None
    match = _MONEY.search(text)
    if not match:
        return None
    raw = match[1].replace(" ", "").strip().rstrip(".")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") == 1 and len(raw.split(".")[1]) == 2 and len(raw.split(".")[0]) <= 2:
        pass  # "R$ 12.50" written in the English style by a careless portal.
    else:
        raw = raw.replace(".", "")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if 0 <= value < Decimal("1000000") else None


def parse_weekly_workload(text: str) -> float | None:
    """Weekly hours. A daily figure is converted only when the week is stated too."""
    if not text:
        return None
    if match := _WEEKLY.search(text):
        hours = float(match[1].replace(",", "."))
        return hours if 1 <= hours <= 60 else None
    daily = _DAILY.search(text)
    days = re.search(r"(\d)\s*(?:dias?|vezes)\s*(?:por\s+semana|semanais|na\s+semana)", text, re.I)
    if daily and days:
        hours = float(daily[1].replace(",", ".")) * int(days[1])
        return hours if 1 <= hours <= 60 else None
    return None


def parse_daily_workload(text: str) -> float | None:
    if match := _DAILY.search(text or ""):
        hours = float(match[1].replace(",", "."))
        return hours if 1 <= hours <= 24 else None
    return None


def parse_vacancies(text: str) -> int | None:
    if match := _VACANCIES.search(text or ""):
        count = int(match[1])
        return count if count <= 5000 else None
    return None


def parse_edital_number(text: str) -> str | None:
    if match := _EDITAL_NUMBER.search(text or ""):
        return f"{int(match[1]):02d}/{match[2]}"
    return None


EDUCATION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "higher_education",
        (
            "nivel superior",
            "ensino superior",
            "curso superior",
            "graduacao",
            "bacharelado",
            "licenciatura",
            "diploma de nivel superior",
        ),
    ),
    (
        "high_school",
        (
            "ensino medio",
            "nivel medio",
            "2 grau",
            "segundo grau",
            "medio completo",
            "medio tecnico",
        ),
    ),
    (
        "primary",
        ("ensino fundamental", "nivel fundamental", "1 grau", "primeiro grau", "alfabetizado"),
    ),
)


def parse_education(text: str) -> str | None:
    """Highest education level demanded. Ambiguity resolves to the stricter requirement."""
    plain = normalize(text or "")
    if not plain:
        return None
    for level, markers in EDUCATION_PATTERNS:
        if any(marker in plain for marker in markers):
            return level
    return None


# Exam location and assignment location are different fields and must never be confused.
_ASSIGNMENT_MARKERS = (
    "lotacao",
    "lotado",
    "local de trabalho",
    "localidade de trabalho",
    "municipio de lotacao",
    "exercicio",
    "unidade de lotacao",
    "sede de trabalho",
    "vaga para",
    "vagas para",
    "distribuicao das vagas",
    "quadro de vagas",
)
_EXAM_MARKERS = (
    "local de prova",
    "locais de prova",
    "local de aplicacao",
    "aplicacao das provas",
    "cidade de prova",
    "realizacao das provas",
    "local de realizacao",
    # Editais phrase this actively at least as often as they use a noun.
    "provas serao aplicadas",
    "prova sera aplicada",
    "aplicadas na cidade",
    "aplicada na cidade",
    "provas serao realizadas",
    "prova sera realizada",
)


# Clause boundaries. Bullet characters matter: edital tables are bulleted lists.
_SENTENCE_END = re.compile(r"[.;:\n\r•●]")


def _sentence_around(text: str, index: int, span: int = 400) -> str:
    """The clause containing `index`, bounded by sentence punctuation.

    A fixed character window is not good enough: "provas aplicadas em Rio Branco. A
    lotacao sera em Cruzeiro do Sul" put an assignment marker within 220 characters of
    the city and produced a false confirmed assignment.
    """
    # With no boundary before `index`, the clause starts at the window edge, not at the
    # match itself -- starting at `index` would hand back only the city name.
    left = max(0, index - span)
    for boundary in _SENTENCE_END.finditer(text, left, index):
        left = boundary.end()
    right = min(len(text), index + span)
    if closing := _SENTENCE_END.search(text, index, right):
        right = closing.start()
    return text[left:right]


def location_context(text: str, city: str) -> tuple[bool, bool, str]:
    """Classify how `city` appears in `text`.

    Returns (mentioned_as_assignment, mentioned_as_exam_only, evidence_snippet). A city
    that only ever appears next to exam wording never counts as a confirmed assignment.
    """
    plain, origins = normalize_indexed(text or "")
    target = normalize(city)
    if not target or target not in plain:
        return False, False, ""
    assignment = exam = False
    snippet = ""
    for match in re.finditer(re.escape(target), plain):
        clause = normalize(_sentence_around(text, origins[match.start()]))
        near_assignment = any(marker in clause for marker in _ASSIGNMENT_MARKERS)
        near_exam = any(marker in clause for marker in _EXAM_MARKERS)
        # Exam wording in the same clause overrides a stray assignment word.
        if near_assignment and not near_exam:
            assignment = True
            snippet = snippet or clause.strip()
        if near_exam:
            exam = True
    if not snippet:
        first = plain.find(target)
        snippet = normalize(_sentence_around(text, origins[first])).strip()
    return assignment, (exam and not assignment), snippet


# A municipal body of a given city has no posts anywhere else, so its own name is
# sufficient evidence of assignment. State and federal bodies are deliberately excluded:
# TJAC has comarcas across Acre and IFAC has campuses in several municipalities, so for
# those the assignment must be stated in the document.
_MUNICIPAL_MARKERS = (
    "prefeitura municipal de",
    "prefeitura de",
    "camara municipal de",
    "camara de vereadores de",
    "municipio de",
    "instituto de previdencia do municipio de",
    "fundacao municipal de",
    "autarquia municipal de",
    "secretaria municipal",
)


def institution_implies_city(institution: str, city: str) -> bool:
    """True when the institution is, by definition, a body of `city`."""
    plain = normalize(institution)
    target = normalize(city)
    if not target or target not in plain:
        return False
    return any(
        marker + " " + target in plain or plain.startswith(marker.strip()) and target in plain
        for marker in _MUNICIPAL_MARKERS
    )


# Documents that use concurso vocabulary but offer no job. Real editais of these kinds
# share a portal with real concursos, so the vocabulary alone cannot be trusted.
NOT_A_JOB_MARKERS = (
    "organizacoes da sociedade civil",
    "chamamento publico",
    "termo de fomento",
    "termo de colaboracao",
    "credenciamento",
    "licitacao",
    "pregao",
    "tomada de precos",
    "concorrencia publica",
    "convenio",
    "dispensa de licitacao",
    "leilao",
    "processo seletivo de alunos",
    "selecao de bolsistas",
    "bolsa de estudo",
    "residencia medica",
    "residencia multiprofissional",
    "programa de estagio",
    "selecao de estagiarios",
    "matricula",
    "vestibular",
    "transferencia interna",
    "remocao interna",
    "concurso de remocao",
    "concurso cultural",
    "premio",
    "concurso de fotografia",
    "outorga de delegacao",
    "servicos notariais",
)
JOB_MARKERS = (
    "cargo",
    "cargos",
    "emprego",
    "empregos",
    "vaga",
    "vagas",
    "remuneracao",
    "vencimento",
    "carga horaria",
    "escolaridade",
    "provimento",
    "contratacao",
    "lotacao",
    "funcao",
)


def is_job_selection(title: str, text: str) -> bool:
    """Whether this document offers paid public work, as opposed to a call for entities,
    a tender, a student selection or an internal transfer."""
    heading = normalize(f"{title or ''} {(text or '')[:1500]}")
    body = normalize((text or "")[:20000])
    if any(marker in heading for marker in NOT_A_JOB_MARKERS):
        return False
    if not any(marker in body for marker in JOB_MARKERS):
        return False
    return True


EMPLOYMENT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Order matters: a document saying "concurso publico" and "temporaria" is checked
    # against the temporary markers first, because temporary wins when both appear.
    (
        "TEMPORARY",
        (
            "contratacao temporaria",
            "por tempo determinado",
            "carater temporario",
            "processo seletivo simplificado",
            "pss ",
            "contrato temporario",
            "temporaria de excepcional interesse",
        ),
    ),
    (
        "PERMANENT",
        (
            "concurso publico",
            "provimento efetivo",
            "cargo efetivo",
            "quadro permanente",
            "estatutario",
            "cargos efetivos",
        ),
    ),
    (
        "PUBLIC_EMPLOYMENT",
        ("emprego publico", "celetista", "regime da clt", "consolidacao das leis do trabalho"),
    ),
    ("SELECTION", ("processo seletivo", "selecao publica", "chamada publica")),
)


def parse_employment_type(text: str) -> str:
    plain = normalize(text or "")
    for kind, markers in EMPLOYMENT_MARKERS:
        if any(marker in plain for marker in markers):
            return kind
    return "UNKNOWN"


# The employment regime decides what the job actually is: a statutory civil servant has
# stability and a different pension; a CLT employee does not. The spec asks for it and it
# is stated plainly in every edital, usually by naming the law that governs it.
REGIME_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Estatutário",
        ("regime juridico estatutario", "regime estatutario", "estatutario dos servidores"),
    ),
    (
        "CLT",
        ("consolidacao das leis do trabalho", "regime da clt", "celetista", "regime celetista"),
    ),
    ("Administrativo especial", ("regime administrativo especial", "regime especial")),
    ("Temporário", ("contratacao temporaria", "por tempo determinado")),
)
_BENEFIT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Auxílio-alimentação",
        (
            "auxilio alimentacao",
            "auxilio refeicao",
            "vale alimentacao",
            "vale refeicao",
            "ticket alimentacao",
        ),
    ),
    ("Auxílio-transporte", ("auxilio transporte", "vale transporte")),
    ("Auxílio-saúde", ("auxilio saude", "plano de saude", "assistencia medica")),
    ("Auxílio-creche", ("auxilio creche", "auxilio pre escolar")),
    ("Gratificação", ("gratificacao de", "adicional de qualificacao")),
)
_EXEMPTION_MARKERS = (
    "isencao da taxa de inscricao",
    "isencao do pagamento da taxa",
    "isencao de taxa de inscricao",
    "podera solicitar isencao",
    "solicitacao de isencao",
)
_EXEMPTION_REASONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("CadÚnico / baixa renda", ("cadastro unico", "cadunico", "baixa renda", "familiar de baixa")),
    ("Doador de sangue", ("doador de sangue", "doacao de sangue")),
    ("Doador de medula", ("doador de medula", "medula ossea")),
    ("Desempregado", ("desempregado", "desempregada")),
    ("Doador de leite", ("doacao de leite",)),
)


def parse_employment_regime(text: str) -> str | None:
    """Which legal regime governs the post, when the edital names it."""
    plain = normalize(text or "")
    for label, markers in REGIME_MARKERS:
        if any(marker in plain for marker in markers):
            return label
    return None


def parse_benefits(text: str) -> str | None:
    """Benefits named in the edital. Real money on top of a modest salary."""
    plain = normalize(text or "")
    found = [
        label for label, markers in _BENEFIT_MARKERS if any(marker in plain for marker in markers)
    ]
    return "; ".join(found) or None


def parse_fee_exemption(text: str) -> str | None:
    """Who may ask for the fee to be waived.

    Directly useful: someone filtering for high-school posts on a study-compatible
    workload is often exactly who qualifies.
    """
    if not text:
        return None
    index = _locate_marker(text, _EXEMPTION_MARKERS)
    if index < 0:
        return None
    window = normalize(text[index : index + 2500])
    reasons = [
        label
        for label, markers in _EXEMPTION_REASONS
        if any(marker in window for marker in markers)
    ]
    return "; ".join(reasons) or "Prevista no edital: consultar condições"


def _locate_marker(text: str, markers: tuple[str, ...]) -> int:
    plain, origins = normalize_indexed(text)
    for marker in markers:
        found = plain.find(marker)
        if found >= 0:
            return origins[found]
    return -1


DOCUMENT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("CANCELLATION", ("cancelamento", "anulacao do edital", "tornar sem efeito", "revogacao")),
    ("SUSPENSION", ("suspensao", "suspenso o certame", "sobrestamento")),
    ("REOPENING", ("reabertura", "reaberto o prazo")),
    ("EXTENSION", ("prorrogacao", "prorrogado o prazo", "prorroga o prazo")),
    ("RECTIFICATION", ("retificacao", "retifica", "errata", "aditamento")),
    ("SUMMONS", ("convocacao", "convoca os candidatos", "nomeacao", "posse")),
    ("RESULT", ("resultado", "classificacao final", "homologacao", "gabarito")),
    ("SCHEDULE", ("cronograma", "calendario de provas")),
    ("CORRECTION", ("correcao", "comunicado de alteracao")),
    ("APPLICATION", ("inscricoes abertas", "abertura das inscricoes")),
    (
        "OPENING",
        (
            "edital de abertura",
            "edital n",
            "abre concurso",
            "torna publica a abertura",
            "torna publico a abertura",
            "abertura de concurso",
        ),
    ),
)


# An opening edital contains a cronograma, a convocacao clause and a resultado clause too,
# so these unambiguous phrases are checked before the generic amendment markers.
STRONG_OPENING = (
    "edital de abertura",
    "torna publica a abertura",
    "torna publico a abertura",
    "torna publicas as normas",
    "abertura de inscricoes",
    "abertura das inscricoes",
    "abre concurso publico",
    "abertura de concurso",
)


# An amendment announces itself in its own heading, so these outrank everything.
AMENDING = ("CANCELLATION", "SUSPENSION", "REOPENING", "EXTENSION", "RECTIFICATION", "CORRECTION")


def classify_document(title: str, text: str = "") -> str:
    """What this document does to an opportunity.

    Precedence in the heading: amendment wording, then opening wording, then everything
    else. An opening edital necessarily contains a cronograma, a convocacao clause and a
    resultado clause, so those generic words must never outrank "edital de abertura".
    """
    header = normalize(f"{title or ''} {(text or '')[:1200]}")
    for kind, markers in DOCUMENT_MARKERS:
        if kind in AMENDING and any(marker in header for marker in markers):
            return kind
    if any(marker in header for marker in STRONG_OPENING):
        return "OPENING"
    for source in (normalize(title or ""), header, normalize((text or "")[:6000])):
        if not source:
            continue
        for kind, markers in DOCUMENT_MARKERS:
            if any(marker in source for marker in markers):
                return kind
    return "UNKNOWN"


_PUBLICATION_MARKERS = (
    "publicado em",
    "publicacao",
    "data de publicacao",
    "dou de",
    "diario oficial de",
)


def parse_publication_date(
    title: str, text: str, fallback_header: str | None = None
) -> date | None:
    """Prefer an explicitly labelled publication date over the first date in the document."""
    plain = text or ""
    normalized, origins = normalize_indexed(plain)
    for marker in _PUBLICATION_MARKERS:
        index = normalized.find(marker)
        if index >= 0 and (value := parse_date(plain[origins[index] : origins[index] + 400])):
            return value
    if value := parse_date(title or ""):
        return value
    if fallback_header and (value := _http_date(fallback_header)):
        return value
    return parse_date(plain[:2500])


def _http_date(value: str) -> date | None:
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%A, %d-%b-%y %H:%M:%S %Z", "%a %b %d %H:%M:%S %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None
