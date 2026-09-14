"""The model locates; deterministic code decides. These tests are the proof."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from sentinela.verify import apply_to_position, quote_is_real, verify

EDITAL = """
CÂMARA MUNICIPAL DE RIO BRANCO — EDITAL Nº 01/2026
O cargo de Agente Legislativo exige diploma de conclusão de curso de nível médio,
com remuneração de R$ 4.656,75 e carga horária de 30 horas semanais, para
provimento de 01 (uma) vaga.
As provas serão aplicadas na cidade de Rio Branco.
"""


def test_a_real_quote_with_a_correct_value_is_confirmed() -> None:
    result = verify(
        "weekly_workload",
        30,
        "carga horária de 30 horas semanais, para provimento",
        EDITAL,
    )
    assert result.confirmed
    assert result.value == 30.0


@pytest.mark.parametrize(
    "field,proposed,expected",
    [
        ("salary", 4656.75, Decimal("4656.75")),
        ("education", "high_school", "high_school"),
        ("vacancies", 1, 1),
    ],
)
def test_other_fields_round_trip(field: str, proposed: object, expected: object) -> None:
    quotes = {
        "salary": "com remuneração de R$ 4.656,75 e carga horária de 30 horas",
        "education": "exige diploma de conclusão de curso de nível médio, com remuneração",
        "vacancies": "para provimento de 01 (uma) vaga. As provas serão aplicadas",
    }
    result = verify(field, proposed, quotes[field], EDITAL)
    assert result.confirmed
    assert result.value == expected


def test_an_invented_quote_is_rejected() -> None:
    """The whole point: a sentence the model made up cannot enter the system."""
    result = verify(
        "weekly_workload",
        20,
        "A carga horária do cargo será de 20 horas semanais conforme o anexo II.",
        EDITAL,
    )
    assert not result.confirmed
    assert result.status == "QUOTE_NOT_FOUND"


def test_a_real_quote_that_does_not_support_the_claim_is_rejected() -> None:
    """The sentence exists, but says 30h. The model claiming 20h does not make it so."""
    result = verify(
        "weekly_workload",
        20,
        "carga horária de 30 horas semanais, para provimento",
        EDITAL,
    )
    assert not result.confirmed
    assert result.status == "PARSER_DISAGREES"
    assert "30" in result.detail


def test_a_quote_with_no_parsable_value_is_rejected() -> None:
    result = verify(
        "weekly_workload",
        30,
        "As provas serão aplicadas na cidade de Rio Branco.",
        EDITAL,
    )
    assert result.status == "PARSER_DISAGREES"


def test_a_quote_too_short_to_prove_anything_is_rejected() -> None:
    assert quote_is_real(EDITAL, "30 horas") is False
    assert verify("weekly_workload", 30, "30 horas", EDITAL).status == "QUOTE_NOT_FOUND"


def test_line_wrapping_and_accents_do_not_break_a_genuine_quote() -> None:
    # A PDF wraps mid-sentence; the quote is still real.
    assert quote_is_real(EDITAL, "carga  horaria   de 30 HORAS\nSEMANAIS, para provimento")


def test_a_field_outside_the_verifiable_set_is_never_applied() -> None:
    assert (
        verify(
            "assignment_location",
            "Rio Branco",
            "As provas serão aplicadas na cidade de Rio Branco.",
            EDITAL,
        ).confirmed
        is False
    )


def test_only_gaps_are_filled_and_known_values_are_never_overwritten() -> None:
    position = SimpleNamespace(weekly_workload=40.0, salary=None, evidence={})
    claims = {
        "weekly_workload": {
            "value": 30,
            "raw_evidence": "carga horária de 30 horas semanais, para",
        },
        "salary": {
            "value": 4656.75,
            "raw_evidence": "com remuneração de R$ 4.656,75 e carga horária",
        },
    }
    apply_to_position(position, claims, EDITAL, "https://x.gov.br/e.pdf", "v1")
    assert position.weekly_workload == 40.0  # deterministic value untouched
    assert position.salary == Decimal("4656.75")  # gap filled, and proven
    assert position.evidence["salary"].extraction_method.startswith("llm_located+parser_verified")
    assert position.evidence["salary"].confidence < 0.95  # below a real table parse
    assert "4.656,75" in position.evidence["salary"].raw_evidence


def test_a_rejected_claim_leaves_the_position_untouched() -> None:
    position = SimpleNamespace(weekly_workload=None, evidence={})
    claims = {
        "weekly_workload": {"value": 20, "raw_evidence": "jornada de 20 horas semanais fixas"}
    }
    apply_to_position(position, claims, EDITAL, "https://x.gov.br/e.pdf", "v1")
    assert position.weekly_workload is None
    assert position.evidence == {}
