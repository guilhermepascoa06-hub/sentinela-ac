"""Golden test against a real, published edital.

Source: Câmara Municipal de Rio Branco, Edital nº 01/2026, published 11/09/2026.
Every expectation below was read by a human from the official PDF. If a parser change
breaks one of these, the change is wrong until proven otherwise.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from sentinela.config import Configuration
from sentinela.eligibility import evaluate
from sentinela.extract import extract
from sentinela.structure import build_opportunity

URL = "https://www.riobranco.ac.leg.br/transparencia/concurso/2026/EDITAL_01.pdf"
INSTITUTION = "Câmara Municipal de Rio Branco"
TODAY = date(2026, 9, 14)


@pytest.fixture(scope="module")
def draft(request: pytest.FixtureRequest):
    from tests.conftest import FIXTURES

    path = FIXTURES / "public_sources" / "camara_rio_branco_2026.pdf"
    if not path.is_file():
        pytest.skip("fixture de edital real ausente")
    content = path.read_bytes()
    document = extract(content, URL, "pdf")
    return build_opportunity(
        document, content, URL, institution=INSTITUTION, city="Rio Branco", media_type="pdf"
    )


def position(draft, name: str):
    for item in draft.positions:
        if item.name.strip().lower() == name.lower():
            return item
    raise AssertionError(
        f"cargo {name!r} não extraído; extraídos: {[item.name for item in draft.positions]}"
    )


def test_pdf_text_extraction_uses_the_native_layer(draft) -> None:
    from tests.conftest import FIXTURES

    content = (FIXTURES / "public_sources" / "camara_rio_branco_2026.pdf").read_bytes()
    document = extract(content, URL, "pdf")
    assert document.status == "OK"
    assert document.method == "pdf_pymupdf"  # no OCR needed for a digital edital
    assert document.page_count == 56
    assert len(document.text) > 150_000


def test_document_level_fields(draft) -> None:
    assert draft.edital_number == "01/2026"
    assert draft.publication_date == date(2026, 9, 11)
    assert draft.employment_type == "PERMANENT"
    assert draft.organizing_board == "IDIB"
    assert draft.document_type == "OPENING"


def test_schedule_comes_from_the_annex_table(draft) -> None:
    # Regression: an "inscrições para candidatos que solicitam isenção" row used to steal
    # the registration window and report 14/09–16/09 instead of the real dates.
    assert draft.registration_start == date(2026, 9, 11)
    assert draft.registration_deadline == date(2026, 10, 13)
    assert draft.exam_date == date(2026, 11, 22)
    assert draft.payment_deadline == date(2026, 10, 14)
    assert draft.fee_exemption_deadline == date(2026, 9, 16)


def test_application_fee_is_the_high_school_row(draft) -> None:
    assert draft.application_fee == Decimal("85.00")  # nível superior pays R$ 95,00


def test_agente_legislativo_matches_the_official_pdf(draft) -> None:
    cargo = position(draft, "Agente Legislativo")
    assert cargo.education == "high_school"
    assert cargo.qualification_category == "HIGH SCHOOL ONLY"
    assert cargo.additional_qualifications == []
    assert cargo.weekly_workload == 30.0
    assert cargo.salary == Decimal("4656.75")
    assert cargo.vacancies == 1
    assert cargo.reserve_count == 2
    assert cargo.assignment_confirmed is True
    assert cargo.assignment_location == "Rio Branco"


def test_higher_education_positions_are_separated(draft) -> None:
    cargo = position(draft, "Analista Legislativo")
    assert cargo.education == "higher_education"
    assert cargo.salary == Decimal("6300.00")


def test_every_evidence_field_quotes_the_document(draft) -> None:
    cargo = position(draft, "Agente Legislativo")
    for name in ("education", "salary", "weekly_workload", "vacancies"):
        evidence = cargo.evidence[name]
        assert evidence.status == "FOUND"
        assert evidence.raw_evidence, f"{name} sem trecho de origem"
        assert evidence.source_url == URL
        assert 0 < evidence.confidence <= 1


def test_eligibility_ranks_the_high_school_cargo_as_excellent(draft) -> None:
    config = Configuration()
    verdict = evaluate(draft, position(draft, "Agente Legislativo"), config, TODAY, trust_level=1)
    assert verdict.eligible is True
    assert verdict.primary_alert is True
    assert verdict.workload_classification == "GOOD"  # 30h is inside the acceptable band
    assert verdict.rank == "EXCELLENT"
    assert verdict.score >= 90


def test_stored_reasons_reach_the_reader_as_words(draft) -> None:
    """The panel, the bot card and the alert print these strings verbatim. "Carga horária:
    GOOD" was a stored code answering a person's question about why the cargo fits."""
    from sentinela.alerts import WORKLOAD_LABEL

    verdict = evaluate(
        draft, position(draft, "Agente Legislativo"), Configuration(), TODAY, trust_level=1
    )
    workload = [reason for reason in verdict.reasons if reason.startswith("Carga horária")]
    assert workload == [f"Carga horária: {WORKLOAD_LABEL['GOOD']}"]
    assert "GOOD" not in workload[0]


def test_higher_education_cargo_is_not_eligible(draft) -> None:
    verdict = evaluate(
        draft, position(draft, "Analista Legislativo"), Configuration(), TODAY, trust_level=1
    )
    assert verdict.eligible is False
    assert verdict.primary_alert is False


def test_notification_body_never_invents_a_missing_value(draft) -> None:
    from types import SimpleNamespace

    from sentinela.alerts import opportunity_message

    cargo = position(draft, "Tradutor Intérprete de Língua Brasileira de Sinais")
    verdict = evaluate(draft, cargo, Configuration(), TODAY, trust_level=1)
    # The notifier reads the merged view the pipeline builds: draft fields plus verdict.
    view = SimpleNamespace(
        **cargo.model_dump(),
        workload_classification=verdict.workload_classification,
        score=verdict.score,
        rank=verdict.rank,
        confidence=verdict.confidence,
        reasons=verdict.reasons,
    )
    message = opportunity_message(draft, view)
    assert "R$ 4.656,75" not in message  # this cargo has no salary cell of its own
    assert "não informado" in message
    assert "30h/semana" in message
