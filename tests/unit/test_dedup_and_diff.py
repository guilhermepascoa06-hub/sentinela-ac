from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from sentinela import diff
from sentinela.dedup import (
    Candidate,
    dedup_key,
    find_match,
    institution_key,
    merge_positions,
    position_slug,
    similarity,
)
from sentinela.domain import OpportunityDraft


def make(
    institution: str,
    name: str,
    edital: str | None = None,
    published: date | None = date(2026, 9, 11),
    board: str | None = None,
) -> OpportunityDraft:
    return OpportunityDraft(
        institution=institution,
        name=name,
        edital_number=edital,
        publication_date=published,
        organizing_board=board,
    )


@pytest.mark.parametrize(
    "institution,expected",
    [
        ("Câmara Municipal de Rio Branco", "camara-rio-branco"),
        ("Prefeitura Municipal de Rio Branco", "prefeitura-rio-branco"),
        ("Tribunal de Justiça do Estado do Acre", "tribunal-justica-acre"),
    ],
)
def test_institution_key(institution: str, expected: str) -> None:
    assert institution_key(institution) == expected


def test_two_bodies_in_the_same_city_do_not_collide() -> None:
    # Regression: stripping "camara"/"prefeitura" as noise collapsed both into "rio-branco".
    left = institution_key("Câmara Municipal de Rio Branco")
    right = institution_key("Prefeitura Municipal de Rio Branco")
    assert left != right


def test_dedup_key_is_exact_when_an_edital_number_exists() -> None:
    a = make("Câmara Municipal de Rio Branco", "Concurso para nível médio", "01/2026")
    b = make("Camara Municipal de Rio Branco", "CONCURSO PÚBLICO CMRB", "01/2026")
    assert dedup_key(a) == dedup_key(b)


def test_dedup_key_separates_different_editais() -> None:
    a = make("Prefeitura Municipal de Rio Branco", "Concurso", "01/2026")
    b = make("Prefeitura Municipal de Rio Branco", "Concurso", "02/2026")
    assert dedup_key(a) != dedup_key(b)


def test_same_concurso_from_four_sources_becomes_one_opportunity() -> None:
    stored = Candidate(
        id="x",
        dedup_key=dedup_key(
            make("Câmara Municipal de Rio Branco", "Concurso público 2026", "01/2026")
        ),
        institution="Câmara Municipal de Rio Branco",
        name="Concurso público 2026",
        edital_number="01/2026",
        publication_date=date(2026, 9, 11),
        organizing_board="IDIB",
        urls=("https://riobranco.ac.leg.br/edital.pdf",),
        document_hashes=("abc123",),
    )
    from_gazette = make("Câmara Municipal de Rio Branco", "Concurso público 2026", "01/2026")
    from_board = make("Camara Municipal Rio Branco", "CMRB — concurso 2026", "01/2026")
    from_aggregator = make("Câmara Municipal de Rio Branco", "Concurso Câmara RB 2026")

    assert find_match(from_gazette, [stored])[1] == "dedup_key"
    assert find_match(from_board, [stored])[1] in ("dedup_key", "edital_number")
    match, reason = find_match(from_aggregator, [stored])
    assert match is stored and reason.startswith("similarity")
    assert find_match(from_aggregator, [stored], document_hash="abc123")[1] == "document_hash"
    assert (
        find_match(from_aggregator, [stored], url="https://riobranco.ac.leg.br/edital.pdf")[1]
        == "url"
    )


def test_different_institutions_never_merge_on_similarity_alone() -> None:
    stored = Candidate(
        id="x",
        dedup_key="k",
        institution="Prefeitura Municipal de Rio Branco",
        name="Concurso público para agente administrativo",
        edital_number=None,
        publication_date=date(2026, 9, 11),
        organizing_board=None,
    )
    other = make(
        "Tribunal de Justiça do Estado do Acre", "Concurso público para agente administrativo"
    )
    assert find_match(other, [stored]) == (None, "new")


def test_different_years_never_merge() -> None:
    stored = Candidate(
        id="x",
        dedup_key="k",
        institution="Prefeitura Municipal de Rio Branco",
        name="Concurso público para agente administrativo",
        edital_number=None,
        publication_date=date(2022, 3, 1),
        organizing_board=None,
    )
    current = make(
        "Prefeitura Municipal de Rio Branco",
        "Concurso público para agente administrativo",
        published=date(2026, 3, 1),
    )
    assert find_match(current, [stored]) == (None, "new")


def test_similarity_is_symmetric_and_bounded() -> None:
    assert similarity("agente legislativo", "agente legislativo") == 1.0
    assert similarity("agente legislativo", "") == 0.0
    assert similarity("a b", "b a") == similarity("b a", "a b")


def test_position_slug_is_stable_across_spelling() -> None:
    assert position_slug("Agente Legislativo") == position_slug("AGENTE  LEGISLATIVO")


def test_merge_positions_never_erases_known_data() -> None:
    existing = [{"slug": "agente", "name": "Agente", "salary": 4656.75, "weekly_workload": 30}]
    incoming = [
        {"slug": "agente", "name": "Agente", "salary": None, "vacancies": 2},
        {"slug": "novo", "name": "Novo cargo"},
    ]
    merged = {item["slug"]: item for item in merge_positions(existing, incoming)}
    assert merged["agente"]["salary"] == 4656.75
    assert merged["agente"]["vacancies"] == 2
    assert "novo" in merged


# ---------------------------------------------------------------- change detection


def test_registration_extension_is_alertable_with_a_readable_diff() -> None:
    before = {"registration_deadline": date(2026, 10, 10)}
    after = {"registration_deadline": date(2026, 10, 17)}
    changes = diff.compare(before, after)
    assert len(changes) == 1
    assert changes[0]["alertable"] is True
    rendered = diff.render(changes, source="TJAC", document="Retificação nº 2")
    assert "PRAZO DE INSCRIÇÃO" in rendered
    assert "10/10/2026" in rendered and "17/10/2026" in rendered
    assert "Antes:" in rendered and "Agora:" in rendered


def test_losing_a_known_value_is_recorded_but_never_alerted() -> None:
    changes = diff.compare({"exam_date": date(2026, 11, 22)}, {"exam_date": None})
    assert changes[0]["information_lost"] is True
    assert changes[0]["alertable"] is False
    assert diff.alertable(changes) == []


def test_cosmetic_changes_are_not_alerts() -> None:
    changes = diff.compare({"name": "Concurso Público"}, {"name": "CONCURSO  PÚBLICO"})
    assert changes == []
    changes = diff.compare({"organizing_board": "IDIB"}, {"organizing_board": "Cebraspe"})
    assert changes and diff.alertable(changes) == []


@pytest.mark.parametrize(
    "field,before,after",
    [
        ("salary", Decimal("4656.75"), Decimal("5000.00")),
        ("vacancies", 1, 5),
        ("weekly_workload", 40.0, 20.0),
        ("assignment_location", "Cruzeiro do Sul", "Rio Branco"),
        ("education", "higher_education", "high_school"),
    ],
)
def test_position_changes_that_matter(field: str, before: object, after: object) -> None:
    changes = diff.compare({field: before}, {field: after}, scope="position", label="Agente")
    assert changes[0]["alertable"] is True
    assert changes[0]["subject"] == "Agente"


def test_new_and_removed_positions() -> None:
    changes = diff.compare_positions(
        [{"slug": "a", "name": "Cargo A"}],
        [{"slug": "b", "name": "Cargo B"}],
    )
    kinds = {(item["label"], item["alertable"]) for item in changes}
    assert ("Novo cargo", True) in kinds
    assert ("Cargo ausente nesta versão", False) in kinds


def test_decimal_and_float_comparison_tolerates_representation() -> None:
    assert diff.compare({"application_fee": Decimal("85.00")}, {"application_fee": 85.0}) == []


def test_status_escalation() -> None:
    assert diff.status_escalated("EDITAL_PUBLISHED", "REGISTRATION_OPEN") is True
    assert diff.status_escalated("REGISTRATION_OPEN", "REGISTRATION_OPEN") is False
    assert diff.status_escalated("REGISTRATION_OPEN", "REGISTRATION_CLOSED") is False
