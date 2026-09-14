from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from sentinela.parse import (
    classify_document,
    location_context,
    parse_daily_workload,
    parse_date,
    parse_dates,
    parse_edital_number,
    parse_education,
    parse_employment_type,
    parse_money,
    parse_period,
    parse_publication_date,
    parse_vacancies,
    parse_weekly_workload,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("publicado em 11/09/2026", date(2026, 9, 11)),
        ("11 de setembro de 2026", date(2026, 9, 11)),
        ("11 de set. de 2026", date(2026, 9, 11)),
        ("2026-10-13", date(2026, 10, 13)),
        ("13.10.2026", date(2026, 10, 13)),
        ("Das 14h00min de 11/09/2026", date(2026, 9, 11)),
        ("edital 01/26 de 05/03/26", date(2026, 3, 5)),
    ],
)
def test_parse_date(text: str, expected: date) -> None:
    assert parse_date(text) == expected


@pytest.mark.parametrize("text", ["página 32", "artigo 37", "31/02/2026", "13/2026", ""])
def test_parse_date_rejects_non_dates(text: str) -> None:
    assert parse_date(text) is None


def test_parse_period_takes_the_extremes() -> None:
    window = "Período de inscrições: das 14h de 11/09/2026 às 23h59 de 13/10/2026"
    assert parse_period(window) == (date(2026, 9, 11), date(2026, 10, 13))


def test_parse_dates_is_ordered_and_deduplicated() -> None:
    found = parse_dates("11/09/2026, 11/09/2026 e 13/10/2026")
    assert found == [date(2026, 9, 11), date(2026, 10, 13)]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("R$ 4.656,75", Decimal("4656.75")),
        ("R$ 1.500", Decimal("1500")),  # regression: this once read as R$ 1,50
        ("R$ 85,00 (oitenta e cinco reais)", Decimal("85.00")),
        ("R$ 12.345.678,90", None),  # above the sanity ceiling
        ("R$ 950", Decimal("950")),
    ],
)
def test_parse_money(text: str, expected: Decimal | None) -> None:
    assert parse_money(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30 horas semanais", 30.0),
        ("carga horária de 20h semanais", 20.0),
        ("40 (quarenta) horas semanais", 40.0),
        ("jornada de 6 horas diárias, 5 dias por semana", 30.0),
        ("12h/semana", 12.0),
    ],
)
def test_parse_weekly_workload(text: str, expected: float) -> None:
    assert parse_weekly_workload(text) == expected


@pytest.mark.parametrize(
    "text", ["prova com duração de 3h", "80 horas semanais", "atendimento 24 horas"]
)
def test_weekly_workload_rejects_implausible(text: str) -> None:
    assert parse_weekly_workload(text) is None


def test_daily_workload() -> None:
    assert parse_daily_workload("jornada de 6 horas diárias") == 6.0


@pytest.mark.parametrize(
    "text,expected",
    [
        ("diploma de conclusão de curso de nível médio", "high_school"),
        ("ensino médio completo", "high_school"),
        ("diploma de nível superior em Direito", "higher_education"),
        ("ensino fundamental completo", "primary"),
        ("cargo de motorista", None),
    ],
)
def test_parse_education(text: str, expected: str | None) -> None:
    assert parse_education(text) == expected


def test_education_prefers_the_stricter_requirement() -> None:
    text = "ensino médio e, para algumas vagas, nível superior"
    assert parse_education(text) == "higher_education"


def test_assignment_and_exam_locations_are_never_confused() -> None:
    exam_only = (
        "As provas serão aplicadas na cidade de Rio Branco. "
        "A lotação será no município de Cruzeiro do Sul."
    )
    assignment, exam, _ = location_context(exam_only, "Rio Branco")
    assert (assignment, exam) == (False, True)

    real = "Quadro de vagas: 2 vagas para lotação em Rio Branco."
    assignment, exam, snippet = location_context(real, "Rio Branco")
    assert assignment is True
    assert exam is False
    assert "lotacao" in snippet


def test_location_absent_city() -> None:
    assert location_context("vagas em Manaus", "Rio Branco") == (False, False, "")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("torna pública a abertura do CONCURSO PÚBLICO para provimento efetivo", "PERMANENT"),
        ("PROCESSO SELETIVO SIMPLIFICADO para contratação temporária", "TEMPORARY"),
        ("seleção de emprego público sob o regime da CLT", "PUBLIC_EMPLOYMENT"),
        ("nada de relevante aqui", "UNKNOWN"),
    ],
)
def test_employment_type(text: str, expected: str) -> None:
    assert parse_employment_type(text) == expected


def test_temporary_wins_over_concurso_wording() -> None:
    text = "Concurso público e processo seletivo simplificado para contratação temporária"
    assert parse_employment_type(text) == "TEMPORARY"


@pytest.mark.parametrize(
    "title,body,expected",
    [
        ("RETIFICAÇÃO Nº 2 DO EDITAL 01/2026", "", "RECTIFICATION"),
        ("", "torna pública a abertura de inscrições do concurso", "OPENING"),
        ("EDITAL DE CONVOCAÇÃO", "", "SUMMONS"),
        ("COMUNICADO DE SUSPENSÃO DO CERTAME", "", "SUSPENSION"),
        ("PRORROGAÇÃO DO PRAZO DE INSCRIÇÕES", "", "EXTENSION"),
    ],
)
def test_classify_document(title: str, body: str, expected: str) -> None:
    assert classify_document(title, body) == expected


def test_opening_edital_is_not_mistaken_for_its_own_cronograma() -> None:
    # Regression: an opening edital contains a cronograma and a convocacao clause, and was
    # being classified SCHEDULE because those words appear before the opening wording.
    body = (
        "EDITAL DE ABERTURA Nº 01/2026. O PRESIDENTE torna pública a abertura de "
        "inscrições. Ver cronograma no Anexo V e a convocação dos aprovados."
    )
    assert classify_document("", body) == "OPENING"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("EDITAL Nº 01/2026", "01/2026"),
        ("Edital de Abertura n. 12/2025", "12/2025"),
        ("edital 3-2026", "03/2026"),
        ("nenhum número aqui", None),
    ],
)
def test_edital_number(text: str, expected: str | None) -> None:
    assert parse_edital_number(text) == expected


def test_vacancies() -> None:
    assert parse_vacancies("provimento de 13 (treze) vagas") == 13
    assert parse_vacancies("Lei 9.999 de 2020") is None


def test_publication_date_prefers_labelled_value() -> None:
    text = "Prova em 22/11/2026.\nData de publicação: 11/09/2026.\n"
    assert parse_publication_date("", text) == date(2026, 9, 11)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Lei Municipal nº 1.794/2009 (Regime Jurídico Estatutário dos Servidores)", "Estatutário"),
        ("contratação sob o regime da CLT", "CLT"),
        ("celetista, nos termos da Consolidação das Leis do Trabalho", "CLT"),
        ("contratação temporária por tempo determinado", "Temporário"),
        ("nada sobre regime aqui", None),
    ],
)
def test_employment_regime(text: str, expected: str | None) -> None:
    """Statutory or CLT decides what the job actually is: stability and pension differ.
    The field was declared, migrated, and never once populated."""
    from sentinela.parse import parse_employment_regime

    assert parse_employment_regime(text) == expected


def test_benefits_are_collected_not_invented() -> None:
    from sentinela.parse import parse_benefits

    texto = "Além do vencimento, auxílio-alimentação e plano de saúde, sem vale-transporte."
    found = parse_benefits(texto)
    assert "Auxílio-alimentação" in found
    assert "Auxílio-saúde" in found
    assert parse_benefits("nenhum benefício nomeado") is None


def test_fee_exemption_reports_who_qualifies() -> None:
    """Directly useful: whoever filters for high-school posts on a low workload is often
    exactly who qualifies for the waiver."""
    from sentinela.parse import parse_fee_exemption

    texto = (
        "5. DA ISENÇÃO DA TAXA DE INSCRIÇÃO. Poderá solicitar isenção o candidato inscrito "
        "no Cadastro Único para Programas Sociais, o doador de sangue e a pessoa desempregada."
    )
    found = parse_fee_exemption(texto)
    assert "CadÚnico" in found and "Doador de sangue" in found and "Desempregado" in found
    assert parse_fee_exemption("edital sem qualquer isenção prevista") is None
