"""Consultation must reach real stored facts, including unavailable and ambiguous cargos."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from sentinela import bot, bot_queries
from sentinela.bot import route
from sentinela.config import Configuration, Secrets
from sentinela.diff import compare
from sentinela.models import Deadline, Opportunity, OpportunityVersion, Position

TODAY = date(2026, 9, 14)
STAMP = datetime(2026, 9, 14, 15, tzinfo=UTC)
OFFICIAL = "https://exemplo.riobranco.ac.gov.br/edital.pdf"


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot, "now", lambda: STAMP)
    monkeypatch.setattr(bot_queries, "now", lambda: STAMP)


def cargo(
    session: Session,
    name: str = "Agente Legislativo",
    *,
    institution: str = "Câmara de Teste",
    eligible: bool = True,
    status: str = "REGISTRATION_OPEN",
    deadline: date | None = TODAY + timedelta(days=30),
) -> tuple[Opportunity, Position]:
    opportunity = Opportunity(
        dedup_key=f"test-{name}-{institution}",
        institution=institution,
        name="Concurso de Teste",
        status=status,
        registration_start=TODAY - timedelta(days=5),
        registration_deadline=deadline,
        exam_date=TODAY + timedelta(days=60),
        official_edital_url=OFFICIAL,
        official_application_url="https://exemplo.riobranco.ac.gov.br/inscricao",
        application_fee=Decimal("0"),
        eligible=eligible,
        needs_review=False,
        first_seen_at=STAMP,
        last_seen_at=STAMP,
    )
    session.add(opportunity)
    session.flush()
    position = Position(
        opportunity_id=opportunity.id,
        slug=name.lower().replace(" ", "-"),
        name=name,
        salary=Decimal("4656.75"),
        weekly_workload=30,
        eligible=eligible,
        requirements="Ensino médio completo e domínio de informática.",
        assignment_location="Rio Branco/AC",
        assignment_confirmed=True,
        vacancies=0,
        reserve_list=True,
        education="high_school",
        qualification_category="HIGH SCHOOL ONLY",
        reasons=["Lotação confirmada em Rio Branco", "Jornada dentro do filtro"],
        needs_review=False,
        evidence={
            "requirements": {
                "status": "FOUND",
                "raw_evidence": "Ensino médio completo e domínio de informática.",
                "page_number": 4,
                "source_url": OFFICIAL,
            }
        },
    )
    session.add(position)
    session.flush()
    return opportunity, position


def ask(session: Session, config: Configuration, text: str) -> str:
    return route(session, config, Secrets(_env_file=None), text)


def test_search_covers_ineligible_and_closed_with_explicit_labels(
    session: Session, config: Configuration
) -> None:
    cargo(session, "Técnico de Informática", eligible=False, deadline=TODAY - timedelta(days=1))
    text = ask(session, config, "/buscar tecnico informatica")
    assert "Técnico de Informática" in text
    assert "fora do filtro ou não confirmado" in text
    assert "Prova marcada" in text
    assert "inscrições encerradas" in text


@pytest.mark.parametrize("term", ["agente legislativo", "CAMARA", "AgEnTe"])
def test_search_is_accent_case_and_word_order_insensitive(
    session: Session, config: Configuration, term: str
) -> None:
    cargo(session)
    assert "Agente Legislativo" in ask(session, config, f"/buscar {term}")


def test_detail_reaches_requirements_evidence_zero_values_and_application_link(
    session: Session, config: Configuration
) -> None:
    _, position = cargo(session)
    text = ask(session, config, f"/cargo {position.id[:8]}")
    assert "Requisitos: Ensino médio completo" in text
    assert "Requisitos, p. 4" in text
    assert "R$ 4.656,75" in text
    assert "Taxa: R$ 0,00" in text
    assert "Vagas: 0 + cadastro de reserva" in text
    assert "Jornada dentro do filtro" in text
    assert OFFICIAL in text
    assert "/inscricao" in text


def test_ambiguous_cargo_requires_selection_instead_of_guessing(
    session: Session, config: Configuration
) -> None:
    _, first = cargo(session)
    _, second = cargo(session, institution="Instituto de Teste")
    text = ask(session, config, "/cargo agente legislativo")
    assert "Encontrei 2 cargos" in text
    assert first.id[:8] in text and second.id[:8] in text
    assert "Requisitos:" not in text


def test_invalid_or_empty_lookup_has_actionable_help(
    session: Session, config: Configuration
) -> None:
    assert "/cargo agente legislativo" in ask(session, config, "/cargo")
    assert "Nenhum cargo" in ask(session, config, "/cargo cargo inexistente")
    assert "Comando não reconhecido" in ask(session, config, "/inventado")


def test_search_pages_do_not_hide_additional_results(
    session: Session, config: Configuration
) -> None:
    positions = [cargo(session, f"Agente {number}")[1] for number in range(7)]
    first = ask(session, config, "/buscar Agente")
    second = ask(session, config, "/buscar Agente --pagina 2")
    assert "7 cargos registrados · página 1/2" in first
    assert "Próxima: /buscar Agente --pagina 2" in first
    assert "página 2/2" in second
    assert positions[-1].id[:8] not in first
    assert positions[-1].id[:8] in second
    assert "tem 2 página(s)" in ask(session, config, "/buscar Agente --pagina 9")
    assert "a partir de 1" in ask(session, config, "/buscar Agente --pagina 0")


@pytest.mark.parametrize("status", ["SUSPENDED", "CANCELLED", "EXPIRED", "HOMOLOGATED"])
def test_inactive_certames_never_appear_as_available_or_actionable_deadlines(
    session: Session, config: Configuration, status: str
) -> None:
    opportunity, _ = cargo(session, status=status)
    session.add(
        Deadline(
            opportunity_id=opportunity.id,
            kind="registration",
            due_date=TODAY,
            active=True,
            updated_at=STAMP,
        )
    )
    session.flush()
    # Regression: active Deadline rows remained listed even after official suspension.
    assert "Nenhuma vaga compatível" in ask(session, config, "/vagas")
    assert "Nenhum prazo" in ask(session, config, "/prazos")
    assert "Agente Legislativo" in ask(session, config, "/buscar agente")


def test_stale_open_status_is_recomputed_without_changing_stored_data(
    session: Session, config: Configuration
) -> None:
    opportunity, position = cargo(session, deadline=TODAY - timedelta(days=1))
    # The bot runs hourly but collectors run daily: display cannot trust yesterday's status.
    assert "Nenhuma vaga compatível" in ask(session, config, "/vagas")
    text = ask(session, config, f"/cargo {position.id}")
    assert "Situação: Prova marcada" in text
    assert "13/09/2026" in text
    assert opportunity.status == "REGISTRATION_OPEN"


def test_deadline_today_is_still_available(session: Session, config: Configuration) -> None:
    cargo(session, deadline=TODAY)
    assert "Agente Legislativo" in ask(session, config, "/vagas")


def test_factual_natural_question_needs_no_provider(
    session: Session, config: Configuration, monkeypatch: pytest.MonkeyPatch
) -> None:
    cargo(session)

    def fail(*args: object) -> str:
        pytest.fail("deterministic cargo questions must not call the LLM")

    monkeypatch.setattr(bot, "answer_free_text", fail)
    assert "R$ 4.656,75" in ask(session, config, "qual o salário do agente legislativo?")
    assert "Prova: 13/11/2026" in ask(session, config, "quando é a prova?")
    assert "/cargo" in ask(session, config, "oi")


def test_unknown_values_and_unverified_evidence_are_never_filled_in(
    session: Session, config: Configuration
) -> None:
    opportunity, position = cargo(session)
    position.salary = None
    position.requirements = None
    opportunity.application_fee = None
    position.evidence = {
        "requirements": {
            "status": "AMBIGUOUS",
            "raw_evidence": "Trecho não confirmado",
            "page_number": 9,
        }
    }
    text = ask(session, config, f"/cargo {position.id}")
    assert "Salário: não informado" in text
    assert "Requisitos: não informados" in text
    assert "Taxa: não informado" in text
    assert "Trecho não confirmado" not in text


def test_history_uses_the_actual_persisted_diff_shape_and_source(
    session: Session, config: Configuration
) -> None:
    opportunity, position = cargo(session)
    changes = compare(
        {"registration_deadline": "2026-09-20"}, {"registration_deadline": "2026-10-01"}
    )
    session.add(
        OpportunityVersion(
            opportunity_id=opportunity.id,
            version=2,
            detected_at=STAMP,
            changes=changes,
            snapshot={},
            source_url=OFFICIAL,
        )
    )
    session.flush()
    text = ask(session, config, f"/historico {position.id[:8]}")
    assert "Versão 2 · 14/09/2026 10:00" in text
    assert "20/09/2026 → 01/10/2026" in text
    assert OFFICIAL in text


def test_history_never_mixes_cargos_without_subjects(
    session: Session, config: Configuration
) -> None:
    opportunity, position = cargo(session)
    changes = compare({"salary": 3000}, {"salary": 4000}, scope="position", label="Outro Cargo")
    session.add(
        OpportunityVersion(
            opportunity_id=opportunity.id,
            version=1,
            detected_at=STAMP,
            changes=changes,
            snapshot={},
        )
    )
    session.flush()
    text = ask(session, config, f"/historico {position.id[:8]}")
    assert "Outro Cargo" in text
    assert "R$ 3.000,00 → R$ 4.000,00" in text


def test_missing_history_is_explicit(session: Session, config: Configuration) -> None:
    _, position = cargo(session)
    assert "Não há versões registradas" in ask(session, config, f"/historico {position.id[:8]}")


def test_long_card_respects_telegram_utf16_limit(session: Session, config: Configuration) -> None:
    opportunity, position = cargo(session)
    position.requirements = "🧭" * 10000
    # The column caps the address at 1024 characters. PostgreSQL enforces it and SQLite
    # does not: a fixture over the bound passed locally and broke the CI.
    opportunity.official_application_url = "https://example.test/" + "a" * 1000
    text = ask(session, config, f"/cargo {position.id[:8]}")
    assert len(text.encode("utf-16-le")) // 2 <= bot.MAX_REPLY
    assert "[mensagem truncada]" in text


def test_the_code_printed_on_the_card_is_the_code_that_works(
    session: Session, config: Configuration
) -> None:
    """Every card and every list prints the code as "#88fce2b8". Typing it back exactly as
    shown found nothing, because the hash never matched the id."""
    _, position = cargo(session)
    code = position.id[:8]
    assert "Agente Legislativo" in ask(session, config, f"/cargo #{code}")
    assert "Agente Legislativo" in ask(session, config, f"/cargo {code}")
    assert "Histórico" in ask(session, config, f"/historico #{code}") or "Não há versões" in ask(
        session, config, f"/historico #{code}"
    )


def test_history_spends_its_window_on_real_changes(session: Session, config: Configuration) -> None:
    """Re-reading the same document creates a version with no field changes. Seven of them
    filled the whole window and pushed the only real change out of the answer."""
    opportunity, position = cargo(session)
    for number in range(1, 8):
        session.add(
            OpportunityVersion(
                opportunity_id=opportunity.id,
                version=number,
                detected_at=STAMP,
                changes=[],
                snapshot={},
            )
        )
    session.add(
        OpportunityVersion(
            opportunity_id=opportunity.id,
            version=8,
            detected_at=STAMP,
            changes=compare({"status": "EDITAL_PUBLISHED"}, {"status": "REGISTRATION_OPEN"}),
            snapshot={},
        )
    )
    session.flush()
    text = ask(session, config, f"/historico {position.id[:8]}")
    assert "Edital publicado → Inscrições abertas" in text
    assert "7 releituras do documento não mudaram nenhum campo." in text
    assert "sem mudanças de campo" not in text


def test_history_without_any_change_says_so_instead_of_listing_noise(
    session: Session, config: Configuration
) -> None:
    opportunity, position = cargo(session)
    session.add(
        OpportunityVersion(
            opportunity_id=opportunity.id,
            version=1,
            detected_at=STAMP,
            changes=[],
            snapshot={},
        )
    )
    session.flush()
    text = ask(session, config, f"/historico {position.id[:8]}")
    assert "Nenhuma alteração de campo foi registrada" in text
    assert "Uma releitura do documento não mudou nenhum campo." in text
