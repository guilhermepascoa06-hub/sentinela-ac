"""Read-only, deterministic consultation of recorded facts, without scraping or a model."""

from __future__ import annotations

import re
from datetime import date

import sqlalchemy as sa
from sqlalchemy.orm import Session

from sentinela.alerts import (
    EMPLOYMENT_LABEL,
    STATUS_LABEL,
    TRUNCATION_NOTE,
    _truncate,
    day,
    money,
    period,
)
from sentinela.config import Configuration
from sentinela.domain import normalize, now, utc
from sentinela.eligibility import effective_status
from sentinela.models import Opportunity, OpportunityVersion, Position, Tracking
from sentinela.tracking import (
    NEXT_STEP,
    by_position,
    followed,
    forget,
    mark,
)
from sentinela.tracking import (
    STATUS_LABEL as TRACKING_LABEL,
)

Pair = tuple[Opportunity, Position]
PAGE_SIZE = 5
INACTIVE_STATUSES = {"CANCELLED", "SUSPENDED", "HOMOLOGATED", "EXPIRED"}
# Telegram refuses over 4096 UTF-16 units. The margin absorbs the entity expansion the
# API applies to links. bot.MAX_REPLY is this same budget; there is one definition.
MAX_REPLY = 3_900


# Palavras que nunca ajudam a identificar o cargo de que a pergunta fala.
STOPWORDS = {
    "qual",
    "quais",
    "quanto",
    "quantos",
    "quando",
    "onde",
    "como",
    "que",
    "o",
    "a",
    "os",
    "as",
    "e",
    "do",
    "da",
    "dos",
    "das",
    "de",
    "em",
    "no",
    "na",
    "para",
    "por",
    "um",
    "uma",
    "tem",
    "ter",
    "precisa",
    "preciso",
    "sao",
    "sera",
    "ser",
    "saber",
    "salario",
    "salarios",
    "ganha",
    "remuneracao",
    "requisitos",
    "requisito",
    "jornada",
    "horas",
    "prova",
    "taxa",
    "isencao",
    "lotacao",
    "inscricao",
    "data",
    "valor",
    "cargo",
    "me",
    "diga",
    "favor",
    "quero",
    "semanal",
    "semanas",
    "semana",
    "pagar",
    "vale",
    "pena",
    "acha",
    "melhor",
    "sobre",
    "concurso",
    "concursos",
    "cargos",
    "voce",
    "eu",
    "meu",
    "minha",
    "diferenca",
    "entre",
    "mais",
    "menos",
    "posso",
    "devo",
    "fazer",
    "ultimo",
    "ultima",
}


def today(config: Configuration) -> date:
    return now().astimezone(config.zone).date()


def accepting_candidates(opportunity: Opportunity, current: date) -> bool:
    """Compatibility is independent of dates; 'available now' must account for both."""
    return effective_status(opportunity, current) not in (
        INACTIVE_STATUSES | {"REGISTRATION_CLOSED", "EXAM_SCHEDULED"}
    ) and not (opportunity.registration_deadline and opportunity.registration_deadline < current)


def _short(value: object, limit: int = 240) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _Fields:
    """Shortens values while remembering that something was left out.

    A requirement cut mid-sentence reads like the whole requirement to whoever is deciding
    whether to apply. The card has to say that it is hiding text, not imply it with an
    ellipsis.
    """

    def __init__(self) -> None:
        self.omitted = False

    def __call__(self, value: object, limit: int = 240) -> str:
        text = " ".join(str(value).split())
        if len(text) <= limit:
            return text
        self.omitted = True
        return text[: limit - 1].rstrip() + "…"


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _fit(body: list[str], tail: list[str], *, omitted: bool, limit: int = MAX_REPLY) -> str:
    """Evidence and links close the card and are reserved: only the lines above them drop.

    Cutting the message at the end would eat exactly what makes the answer checkable — the
    excerpt, the page and the official address.
    """
    tail_text = "\n".join(tail)
    budget = limit - _units(tail_text) - _units(TRUNCATION_NOTE) - 2
    kept: list[str] = []
    used = 0
    for line in body:
        cost = _units(line) + 1
        if used + cost > budget:
            omitted = True
            break
        kept.append(line)
        used += cost
    message = "\n".join([*kept, *([TRUNCATION_NOTE] if omitted else []), *tail])
    return message if _units(message) <= limit else _truncate(message, limit)


def _rows(session: Session, term: str = "") -> list[Pair]:
    rows = session.execute(
        sa.select(Opportunity, Position)
        .join(Position, Position.opportunity_id == Opportunity.id)
        .where(Position.synthetic.is_(False))
        .order_by(Position.score.desc(), Opportunity.institution, Position.name, Position.id)
    ).all()
    pairs = [(row[0], row[1]) for row in rows]
    query = normalize(term)
    if not query:
        return pairs
    # Exact or unambiguous prefix IDs remain stable when rankings change. Every card
    # prints the code as "#88fce2b8", so that is what gets typed back.
    id_query = term.strip().lstrip("#").lower()
    if len(id_query) >= 8 and re.fullmatch(r"[0-9a-f-]+", id_query):
        return [(o, p) for o, p in pairs if p.id.startswith(id_query)]
    tokens = query.split()
    return [
        (o, p)
        for o, p in pairs
        if all(token in normalize(f"{p.name} {o.institution} {o.name}") for token in tokens)
    ]


def _summary(opportunity: Opportunity, position: Position, current: date) -> str:
    state = effective_status(opportunity, current)
    match = "compatível com o filtro" if position.eligible else "fora do filtro ou não confirmado"
    workload = (
        f"{position.weekly_workload:g}h/semana" if position.weekly_workload else "CH não informada"
    )
    return (
        f"• {_short(position.name, 90)} · #{position.id[:8]}\n"
        f"  {_short(opportunity.institution, 100)}\n"
        f"  {match} · {STATUS_LABEL.get(state, state)}\n"
        f"  {money(position.salary)} · {workload}\n"
        f"  Inscrições: {period(opportunity.registration_start, opportunity.registration_deadline)}"
    )


def _choices(rows: list[Pair], config: Configuration) -> str:
    parts = [
        f"Encontrei {len(rows)} cargos. Escolha pelo nome completo ou pelo código:",
        *[_summary(o, p, today(config)) for o, p in rows[:PAGE_SIZE]],
        "Use /cargo CÓDIGO ou /historico CÓDIGO.",
    ]
    if len(rows) > PAGE_SIZE:
        parts.append("Refine a busca por cargo e órgão; /buscar TERMO permite paginar.")
    return "\n\n".join(parts)


def _resolve(session: Session, config: Configuration, term: str) -> Pair | str:
    if not term.strip():
        return "Informe um cargo, órgão ou código. Exemplo: /cargo agente legislativo. Use /buscar para listar."
    rows = _rows(session, term)
    if not rows:
        return (
            "Nenhum cargo registrado corresponde à busca. Use /buscar com parte do nome ou /ajuda."
        )
    if len(rows) != 1:
        return _choices(rows, config)
    return rows[0]


def answer_search(session: Session, config: Configuration, query: str) -> str:
    term = query.strip()[:500]
    page = 1
    if "--pagina" in term:
        match = re.fullmatch(r"(.*?)\s*--pagina\s+(\d+)", term)
        if not match or int(match[2]) < 1:
            return "Use /buscar TERMO --pagina N, com N a partir de 1."
        term, page = match[1].strip(), int(match[2])
    rows = _rows(session, term)
    if not rows:
        return "Nenhum cargo registrado corresponde à busca. Use /buscar sem termo para listar ou /ajuda."
    pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    if page > pages:
        return f"A busca tem {pages} página(s). Use /buscar {term} --pagina {pages}."
    start = (page - 1) * PAGE_SIZE
    parts = [f"{len(rows)} cargos registrados · página {page}/{pages}"]
    parts += [_summary(o, p, today(config)) for o, p in rows[start : start + PAGE_SIZE]]
    parts.append("A busca inclui cargos fora do filtro e inscrições encerradas. Use /cargo CÓDIGO.")
    if page < pages:
        parts.append(f"Próxima: /buscar {term} --pagina {page + 1}")
    return "\n\n".join(parts)


def _evidence_lines(opportunity: Opportunity, position: Position) -> list[str]:
    lines: list[str] = []
    for field, label in (
        ("requirements", "Requisitos"),
        ("salary", "Salário"),
        ("weekly_workload", "Jornada"),
        ("assignment_location", "Lotação"),
    ):
        evidence = (position.evidence or {}).get(field)
        if not isinstance(evidence, dict) or evidence.get("status") != "FOUND":
            continue
        excerpt = evidence.get("raw_evidence")
        if not excerpt:
            continue
        page = evidence.get("page_number")
        where = f", p. {page}" if page is not None else ""
        source = evidence.get("source_url") or opportunity.official_edital_url
        lines.append(f"{label}{where}: “{_short(excerpt, 130)}”")
        if isinstance(source, str) and source.startswith(("https://", "http://")):
            lines.append(source)
        # Keep Telegram cards readable and leave room for the actual requirements.
        if len(lines) >= 4:
            break
    return lines or ["Trecho com página não registrado. Consulte o edital oficial."]


def answer_card(session: Session, config: Configuration, term: str) -> str:
    found = _resolve(session, config, term)
    if isinstance(found, str):
        return found
    opportunity, position = found
    short = _Fields()
    state = effective_status(opportunity, today(config))
    hours = f"{position.weekly_workload:g}h/semana" if position.weekly_workload else "não informada"
    lines = [
        f"{short(position.name, 100)} · #{position.id[:8]}",
        short(opportunity.institution, 140),
        f"Situação: {STATUS_LABEL.get(state, state)}",
        "Filtro: " + ("compatível" if position.eligible else "fora do filtro ou não confirmado"),
        f"Salário: {money(position.salary)} · Jornada: {hours}",
        f"Benefícios: {short(position.benefits or 'não informados', 150)}",
        f"Lotação: {short(position.assignment_location or 'não confirmada', 160)}"
        + (" (confirmada)" if position.assignment_confirmed else " (sem confirmação)"),
        f"Vagas: {position.vacancies if position.vacancies is not None else 'não informadas'}"
        + (" + cadastro de reserva" if position.reserve_list else ""),
        f"Requisitos: {short(position.requirements or 'não informados', 500)}",
        f"Adicionais: {short('; '.join(position.additional_qualifications or []) or 'nenhum registrado', 200)}",
        f"Inscrições: {period(opportunity.registration_start, opportunity.registration_deadline)}",
        f"Prova: {day(opportunity.exam_date)}",
        f"Taxa: {money(opportunity.application_fee)} · Pagamento: {day(opportunity.payment_deadline)}",
        f"Isenção: {short(opportunity.fee_exemption or 'não informada', 150)}"
        f" · Prazo: {day(opportunity.fee_exemption_deadline)}",
        f"Vínculo: {EMPLOYMENT_LABEL.get(opportunity.employment_type, 'não informado')}",
        f"Avaliação: {position.score}/100 · confiança {position.confidence}",
        f"Motivos: {short('; '.join(position.reasons or []) or 'não registrados', 240)}",
    ]
    if position.needs_review or opportunity.needs_review or opportunity.conflicts:
        lines.append("Há dados pendentes de revisão; confira as condições no edital.")
    tail = ["", "Evidência registrada:", *_evidence_lines(opportunity, position)]
    link = opportunity.official_application_url or opportunity.official_edital_url
    if link:
        label = "Inscrição oficial:" if opportunity.official_application_url else "Edital oficial:"
        # A link cut to fit is a wrong link, not a shorter one, and half the message is
        # already a generous budget for evidence plus address.
        if _units("\n".join([*tail, label, link])) <= MAX_REPLY // 2:
            tail += [label, link]
        else:
            tail.append(f"{label} endereço longo demais para o Telegram; veja no edital.")
    mine = by_position(session, [position.id]).get(position.id)
    if mine:
        lines.append(f"Você marcou: {TRACKING_LABEL[mine.status]}")
        if mine.note:
            lines.append(f"Sua anotação: {short(mine.note, 200)}")
    code = position.id[:8]
    tail += [
        "",
        f"Alterações: /historico {code}",
        f"Acompanhar: /salvar {code} · /inscrito {code} · /descartar {code}",
    ]
    return _fit(lines, tail, omitted=short.omitted)


def answer_history(session: Session, config: Configuration, term: str) -> str:
    found = _resolve(session, config, term)
    if isinstance(found, str):
        return found
    opportunity, position = found
    stored = session.scalars(
        sa.select(OpportunityVersion)
        .where(OpportunityVersion.opportunity_id == opportunity.id)
        .order_by(OpportunityVersion.version.desc())
    ).all()
    if not stored:
        return "Não há versões registradas para esse certame. Nenhuma alteração será inferida."
    # Re-reading the same document is how the monitor proves it is alive, but a window of
    # five versions spent on re-reads hides the change the reader came here for.
    versions = [version for version in stored if version.changes]
    quiet = len(stored) - len(versions)
    quiet_line = (
        ""
        if not quiet
        else "Uma releitura do documento não mudou nenhum campo."
        if quiet == 1
        else f"{quiet} releituras do documento não mudaram nenhum campo."
    )
    if not versions:
        return "\n".join(
            [
                f"Histórico do certame · {_short(opportunity.institution, 120)}",
                "Nenhuma alteração de campo foi registrada até agora; os valores atuais são os",
                "da primeira leitura.",
                quiet_line,
            ]
        ).strip()
    lines = [
        f"Histórico do certame · {_short(opportunity.institution, 120)}",
        f"Cargo consultado: {_short(position.name, 100)}",
        f"Até 5 alterações mais recentes, de {len(versions)} registradas; "
        "datas de detecção do monitor.",
    ]
    for version in versions[:5]:
        lines += [
            "",
            f"Versão {version.version} · {utc(version.detected_at).astimezone(config.zone):%d/%m/%Y %H:%M}",
        ]
        changes = version.changes or []
        for change in changes[:4]:
            label = change.get("label") or change.get("field") or "Campo"
            subject = f" ({_short(change['subject'], 65)})" if change.get("subject") else ""
            # These are the human-readable values persisted by diff.compare(), not
            # raw model text. Include the subject to avoid mixing different cargos.
            old = _short(change.get("old_value", "não informado"), 120)
            new = _short(change.get("new_value", "não informado"), 120)
            lines.append(f"• {_short(label, 90)}{subject}: {old} → {new}")
        if len(changes) > 4:
            lines.append(f"Mais {len(changes) - 4} mudanças nesta versão; consulte o documento.")
        if version.source_url:
            lines.append(version.source_url)
    if quiet_line:
        lines += ["", quiet_line]
    return "\n".join(lines)


def answer_question(session: Session, config: Configuration, question: str) -> str | None:
    """Recognize factual questions only; never guess a missing fact or a named cargo."""
    normalized = normalize(question)
    if not any(
        word in normalized.split()
        for word in (
            "salario",
            "salarios",
            "ganha",
            "remuneracao",
            "requisitos",
            "requisito",
            "jornada",
            "horas",
            "prova",
            "taxa",
            "isencao",
            "lotacao",
            "inscricao",
        )
    ):
        return None
    term = " ".join(word for word in normalized.split() if word not in STOPWORDS)
    if term:
        return answer_card(session, config, term)
    rows = [
        (o, p) for o, p in _rows(session) if p.eligible and accepting_candidates(o, today(config))
    ]
    if not rows:
        return "Não há cargo disponível para identificar nessa pergunta. Informe o nome com /cargo ou veja /ajuda."
    if len(rows) == 1:
        return answer_card(session, config, rows[0][1].id)
    return _choices(rows, config)


# ---------------------------------------------------------------- acompanhamento


def _dates(opportunity: Opportunity) -> str:
    return (
        f"Inscrições: {period(opportunity.registration_start, opportunity.registration_deadline)}"
        f" · Prova: {day(opportunity.exam_date)}"
    )


def answer_mark(session: Session, config: Configuration, term: str, status: str) -> str:
    """Grava a decisão dele. Nada aqui altera o que o edital diz."""
    found = _resolve(session, config, term)
    if isinstance(found, str):
        return found
    opportunity, position = found
    row = mark(session, position.id, status)
    lines = [
        f"Anotado: {_short(position.name, 100)} · #{position.id[:8]}",
        _short(opportunity.institution, 140),
        f"Como está para você: {TRACKING_LABEL[row.status]}",
        NEXT_STEP[row.status],
        _dates(opportunity),
    ]
    if row.note:
        lines.append(f"Sua anotação: {_short(row.note, 200)}")
    lines += ["", f"Anotar algo: /nota {position.id[:8]} sua observação", "Ver tudo: /meus"]
    return "\n".join(lines)


def answer_note(session: Session, config: Configuration, argument: str) -> str:
    parts = argument.strip().split(maxsplit=1)
    if len(parts) < 2:
        return (
            "Use /nota CÓDIGO seguido do texto. Exemplo: /nota 048e6507 estudar informática.\n"
            "O código aparece na ficha do cargo, em /vagas e em /buscar."
        )
    found = _resolve(session, config, parts[0])
    if isinstance(found, str):
        return found
    opportunity, position = found
    row = mark(session, position.id, _current_status(session, position.id), parts[1][:1000])
    return "\n".join(
        [
            f"Anotação salva em {_short(position.name, 100)} · #{position.id[:8]}",
            _short(opportunity.institution, 140),
            f"Como está para você: {TRACKING_LABEL[row.status]}",
            f"Anotação: {_short(row.note, 400)}",
            "",
            "Ver tudo: /meus",
        ]
    )


def _current_status(session: Session, position_id: str) -> str:
    existing = by_position(session, [position_id]).get(position_id)
    return existing.status if existing else "INTERESTED"


def answer_forget(session: Session, config: Configuration, term: str) -> str:
    found = _resolve(session, config, term)
    if isinstance(found, str):
        return found
    _, position = found
    removed = forget(session, position.id)
    if not removed:
        return f"{_short(position.name, 100)} não estava na sua lista. Nada mudou."
    return "\n".join(
        [
            f"Removido da sua lista: {_short(position.name, 100)}",
            "A anotação foi apagada junto. O cargo continua sendo monitorado normalmente.",
        ]
    )


def _entry(row: Tracking, opportunity: Opportunity, position: Position, current: date) -> str:
    state = effective_status(opportunity, current)
    lines = [
        f"• {_short(position.name, 90)} · #{position.id[:8]} — {TRACKING_LABEL[row.status]}",
        f"  {_short(opportunity.institution, 100)}",
        f"  {STATUS_LABEL.get(state, state)} · {_dates(opportunity)}",
    ]
    if row.status != "DISMISSED":
        lines.append(f"  {NEXT_STEP[row.status]}")
    if row.note:
        lines.append(f"  Anotação: {_short(row.note, 200)}")
    return "\n".join(lines)


def answer_followed(session: Session, config: Configuration) -> str:
    rows = followed(session)
    if not rows:
        return (
            "Você ainda não está acompanhando nenhum cargo.\n\n"
            "Use /salvar CÓDIGO para ficar de olho em um, ou /inscrito CÓDIGO quando "
            "já tiver feito a inscrição. O código aparece em /vagas e em /buscar."
        )
    current = today(config)
    session_map = {position.id: position for _row, position in rows}
    opportunities = {
        position.opportunity_id: position.opportunity for position in session_map.values()
    }
    active = [(row, position) for row, position in rows if row.status != "DISMISSED"]
    dropped = [(row, position) for row, position in rows if row.status == "DISMISSED"]
    parts = [f"Você está acompanhando {len(active)} cargo(s)."]
    parts += [
        _entry(row, opportunities[position.opportunity_id], position, current)
        for row, position in active[:10]
    ]
    if len(active) > 10:
        parts.append(f"Mais {len(active) - 10} não couberam nesta lista.")
    if dropped:
        nomes = "; ".join(_short(position.name, 60) for _row, position in dropped[:5])
        parts.append(f"Descartados ({len(dropped)}): {nomes}. Não aviso mais sobre eles.")
    parts.append("Mudar: /salvar CÓDIGO · /inscrito CÓDIGO · /esquecer CÓDIGO")
    return _truncate("\n\n".join(parts), MAX_REPLY)


def relevant(session: Session, question: str, limit: int = 6) -> list[Pair]:
    """Cargos que a pergunta parece mencionar.

    Sem isto o modelo só enxergava os compatíveis e respondia "não tenho essa informação"
    para qualquer pergunta sobre um cargo fora do filtro, que está guardado e citável.
    """
    term = " ".join(word for word in normalize(question).split() if word not in STOPWORDS)
    if not term:
        return []
    rows = _rows(session, term)
    if not rows:
        # "vale a pena o tradutor?" tem uma palavra que identifica alguma coisa; "qual a
        # diferenca entre o agente e o analista" tem duas, e parar na primeira fazia o
        # modelo responder que o segundo cargo nao estava registrado.
        seen: set[str] = set()
        rows = []
        for word in term.split():
            if len(word) < 4:
                continue
            for pair in _rows(session, word):
                if pair[1].id not in seen:
                    seen.add(pair[1].id)
                    rows.append(pair)
    return rows[:limit]
