"""O que o usuário decidiu sobre cada cargo, separado do que o edital diz.

Uma decisão pessoal nunca vira fato oficial: marcar "já me inscrevi" não altera o status
do certame, não entra em versão e não é evidência de nada. O efeito é só sobre o que o
sistema fala com ele — e sobre o que para de falar.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from sentinela.domain import now
from sentinela.models import Position, Tracking

STATUS_LABEL = {
    "INTERESTED": "de olho",
    "REGISTERED": "inscrição feita",
    "DISMISSED": "descartado",
}
NEXT_STEP = {
    "INTERESTED": "Próximo passo: decidir e se inscrever antes do prazo.",
    "REGISTERED": "Próximo passo: pagar a taxa (se ainda não pagou) e estudar para a prova.",
    "DISMISSED": "Você descartou este cargo; não vou mais lembrar dos prazos dele.",
}


def mark(session: Session, position_id: str, status: str, note: str | None = None) -> Tracking:
    """Cria ou atualiza a decisão. Anotação vazia não apaga a anotação que já existe."""
    if status not in STATUS_LABEL:
        raise ValueError(f"status desconhecido: {status}")
    row = session.scalar(sa.select(Tracking).where(Tracking.position_id == position_id))
    moment = now()
    if row is None:
        row = Tracking(
            position_id=position_id,
            status=status,
            note=note,
            created_at=moment,
            updated_at=moment,
        )
        session.add(row)
    else:
        row.status = status
        if note is not None:
            row.note = note
        row.updated_at = moment
    session.flush()
    return row


def forget(session: Session, position_id: str) -> bool:
    row = session.scalar(sa.select(Tracking).where(Tracking.position_id == position_id))
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def by_position(session: Session, position_ids: list[str] | None = None) -> dict[str, Tracking]:
    query = sa.select(Tracking)
    if position_ids is not None:
        if not position_ids:
            return {}
        query = query.where(Tracking.position_id.in_(position_ids))
    return {row.position_id: row for row in session.scalars(query)}


def followed(session: Session) -> list[tuple[Tracking, Position]]:
    """O que ele acompanha, do mais recente ao mais antigo, com o cargo junto."""
    rows = session.execute(
        sa.select(Tracking, Position)
        .join(Position, Position.id == Tracking.position_id)
        .order_by(Tracking.updated_at.desc())
    ).all()
    return [(row[0], row[1]) for row in rows]


def dismissed_opportunities(session: Session) -> set[str]:
    """Certames em que todo cargo elegível foi descartado por ele.

    Só aí o silêncio é o que ele pediu: se sobrou um cargo elegível sem descarte, o prazo
    ainda lhe interessa e calar seria decidir por ele.
    """
    rows = session.execute(
        sa.select(Position.opportunity_id, Position.id, Tracking.status)
        .outerjoin(Tracking, Tracking.position_id == Position.id)
        .where(Position.eligible.is_(True), Position.synthetic.is_(False))
    ).all()
    per_opportunity: dict[str, list[str | None]] = {}
    for opportunity_id, _position_id, status in rows:
        per_opportunity.setdefault(opportunity_id, []).append(status)
    return {
        opportunity_id
        for opportunity_id, statuses in per_opportunity.items()
        if statuses and all(status == "DISMISSED" for status in statuses)
    }
