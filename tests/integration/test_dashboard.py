"""The panel must tell the same truth as stored evidence and remain read-only."""

from __future__ import annotations

import re
import threading
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.orm import Session

from sentinela import dashboard
from sentinela.alerts import WORKLOAD_LABEL
from sentinela.config import Configuration
from sentinela.domain import now
from sentinela.models import Deadline, Opportunity, OpportunityVersion, Position


@pytest.fixture
def opportunity(session: Session) -> Opportunity:
    row = Opportunity(
        dedup_key="dashboard-test",
        institution="Câmara de teste",
        name="Concurso de teste",
        status="REGISTRATION_OPEN",
        employment_type="PERMANENT",
        eligible=True,
        registration_start=date(2026, 9, 1),
        registration_deadline=date(2026, 10, 13),
        first_seen_at=now(),
        last_seen_at=now(),
        application_fee=Decimal("0"),
        evidence={
            "registration_deadline": {
                "raw_evidence": "13 de outubro",
                "source_url": "https://example.org/edital",
            }
        },
    )
    row.positions.append(
        Position(
            slug="agente",
            name="Agente",
            eligible=True,
            needs_review=False,
            score=90,
            salary=Decimal("4656.75"),
            vacancies=0,
            requirements="Ensino médio completo",
            evidence={"salary": {"value": "4656.75", "page": 3}},
        )
    )
    session.add(row)
    session.commit()
    return row


@pytest.mark.parametrize("status", ["SUSPENDED", "CANCELLED", "HOMOLOGATED", "EXPIRED"])
def test_inactive_never_looks_open(opportunity: Opportunity, status: str) -> None:
    opportunity.status = status
    data = dashboard.opportunity_data(opportunity, date(2026, 9, 14))
    assert not data["active"]
    assert data["registration_state"] == "INACTIVE"


def test_dates_override_stale_open_status(opportunity: Opportunity) -> None:
    assert dashboard.registration_state(opportunity, date(2026, 10, 14)) == "CLOSED"
    assert dashboard.registration_state(opportunity, date(2026, 10, 13)) == "OPEN"
    assert dashboard.registration_state(opportunity, date(2026, 8, 31)) == "FUTURE"
    opportunity.registration_start = None
    assert dashboard.registration_state(opportunity, date(2026, 10, 13)) == "UNKNOWN"


def test_zero_and_unknown_stay_distinct(opportunity: Opportunity) -> None:
    data = dashboard.opportunity_data(opportunity, date(2026, 9, 14), details=True)
    assert Decimal(data["application_fee"]) == 0
    assert data["positions"][0]["vacancies"] == 0
    assert data["positions"][0]["weekly_workload"] is None
    assert data["positions"][0]["salary"] == "4656.75"
    assert data["positions"][0]["evidence"]["salary"]["page"] == 3


def test_detail_keeps_history_and_evidence(
    session: Session, config: Configuration, opportunity: Opportunity
) -> None:
    session.add(
        OpportunityVersion(
            opportunity_id=opportunity.id,
            version=1,
            detected_at=now(),
            source_url="https://example.org/retificacao",
            changes=[{"field": "salary", "old_value": "R$ 4.000,00", "new_value": "R$ 4.656,75"}],
        )
    )
    session.commit()
    result = dashboard.detail_data(session, config, opportunity.id)
    assert result is not None
    assert result["history"][0]["changes"][0]["new"] == "R$ 4.656,75"
    assert (
        result["opportunity"]["evidence"]["registration_deadline"]["raw_evidence"]
        == "13 de outubro"
    )
    assert dashboard.detail_data(session, config, "00000000-0000-0000-0000-000000000000") is None


def add_deadline(session: Session, opportunity: Opportunity, **kwargs: object) -> Deadline:
    values = dict(
        opportunity_id=opportunity.id,
        kind="REGISTRATION",
        due_date=date(2026, 10, 13),
        active=True,
        description="Prazo de inscrição",
        updated_at=now(),
    )
    values.update(kwargs)
    row = Deadline(**values)
    session.add(row)
    session.commit()
    return row


def test_deadline_selection_excludes_inactive_and_non_eligible(
    session: Session, opportunity: Opportunity
) -> None:
    add_deadline(session, opportunity)
    assert len(dashboard.deadline_data(session, date(2026, 9, 14))) == 1
    opportunity.status = "SUSPENDED"
    session.commit()
    assert dashboard.deadline_data(session, date(2026, 9, 14)) == []
    opportunity.status = "REGISTRATION_OPEN"
    opportunity.positions[0].eligible = False
    session.commit()
    assert dashboard.deadline_data(session, date(2026, 9, 14)) == []
    # Explicitly exporting one inspected concurso may include a non-matching cargo.
    assert len(dashboard.deadline_data(session, date(2026, 9, 14), opportunity.id)) == 1


def test_past_and_superseded_deadlines_excluded(session: Session, opportunity: Opportunity) -> None:
    deadline = add_deadline(session, opportunity)
    assert dashboard.deadline_data(session, date(2026, 10, 14)) == []
    deadline.active = False
    session.commit()
    assert dashboard.deadline_data(session, date(2026, 9, 14)) == []


def test_calendar_all_day_utf8_and_injection(
    session: Session, config: Configuration, opportunity: Opportunity
) -> None:
    today = now().astimezone(config.zone).date()
    due = today + timedelta(days=5)
    deadline = add_deadline(
        session,
        opportunity,
        due_date=due,
        description="Inscrição çã " * 14 + "\r\nBEGIN:VEVENT",
        source_url="javascript:alert(1)",
    )
    content = dashboard.calendar_data(session, config)
    assert content.count("\r\nBEGIN:VEVENT\r\n") == 1
    assert f"DTSTART;VALUE=DATE:{due:%Y%m%d}" in content
    assert f"DTEND;VALUE=DATE:{due + timedelta(days=1):%Y%m%d}" in content
    assert "\r\nURL:javascript" not in content
    assert all(len(line.encode()) <= 75 for line in content.split("\r\n"))
    assert f"UID:{deadline.id}@sentinela-ac.local" in content
    assert dashboard.calendar_data(session, config) == content


def test_summary_counts_real_cargos_not_stale_aggregate(
    session: Session, config: Configuration, opportunity: Opportunity
) -> None:
    data = dashboard.dashboard_data(session, config)
    assert data["summary"]["eligible_positions"] == 1
    assert data["monitor"]["severity"] == "CRITICAL"
    opportunity.positions[0].synthetic = True
    session.commit()
    assert dashboard.dashboard_data(session, config)["summary"]["eligible_positions"] == 0


@pytest.fixture
def http_panel(engine, config):
    server = dashboard.make_server(engine, config, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as client:
        yield client
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_server_blocks_external_origins(http_panel, headers) -> None:
    assert http_panel.get("/api/health", headers=headers).status_code == 403


@pytest.mark.parametrize(
    "path", ["/.env", "/config.yaml", "/assets/../.env", "/api/opportunities/not-a-uuid"]
)
def test_no_arbitrary_files_or_invalid_ids(http_panel, path) -> None:
    response = http_panel.get(path)
    assert response.status_code == 404
    assert "DATABASE_URL" not in response.text


def test_server_no_writes_and_security_headers(http_panel) -> None:
    response = http_panel.get("/api/health")
    assert response.json()["service"] == "sentinela-dashboard"
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert http_panel.post("/api/dashboard", json={"status": "CANCELLED"}).status_code == 501


def test_database_error_is_sanitized(http_panel, monkeypatch) -> None:
    import sqlalchemy as sa

    def unavailable(*args):
        raise sa.exc.SQLAlchemyError("SECRET SHOULD NEVER LEAK")

    monkeypatch.setattr(dashboard, "dashboard_data", unavailable)
    response = http_panel.get("/api/dashboard")
    assert response.status_code == 503
    assert "SECRET" not in response.text


def test_stored_codes_reach_the_panel_as_words(opportunity: Opportunity) -> None:
    """The panel printed "high_school" at a person, who reads codes as broken data."""
    opportunity.positions[0].education = "high_school"
    data = dashboard.opportunity_data(opportunity, date(2026, 9, 14))
    assert data["positions"][0]["education_label"] == "Ensino médio"
    assert data["employment_label"] == "Cargo público efetivo"
    assert data["status_label"] == "Inscrições abertas"


def test_unknown_education_code_is_shown_instead_of_being_invented(
    opportunity: Opportunity,
) -> None:
    opportunity.positions[0].education = "mestrado"
    data = dashboard.opportunity_data(opportunity, date(2026, 9, 14))
    assert data["positions"][0]["education_label"] == "mestrado"


def test_panel_compares_against_values_the_domain_emits() -> None:
    """The workload hint tested for 'IDEAL', which eligibility never produces, so it could
    never appear: the classic defect here is code that is written and never reached."""
    source = (dashboard.WEB / "app.js").read_text(encoding="utf-8")
    tested = set(re.findall(r"workload_classification\s*===?\s*'([A-Z ]+)'", source))
    assert tested, "the panel no longer reads the workload classification"
    assert tested <= set(WORKLOAD_LABEL)
    assert "text(p.education)" not in source, "raw education code back in the panel"
    assert "p.education_label" in source


def test_panel_ships_the_font_its_stylesheet_declares(http_panel) -> None:
    """A declared asset that was never added is a 404 on every page load."""
    declared = re.findall(
        r'url\("(/assets/[^"]+)"\)', (dashboard.WEB / "styles.css").read_text(encoding="utf-8")
    )
    assert declared
    for path in declared:
        response = http_panel.get(path)
        assert response.status_code == 200, path
        assert response.headers["Content-Type"] == "font/woff2"
