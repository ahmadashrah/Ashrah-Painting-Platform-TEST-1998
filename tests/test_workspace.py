"""CRM, memory, tools and reporting against a temp workspace."""

from __future__ import annotations

from lumia.domain.accounts import Account, AccountType, Channel, Confidence, Interaction, PipelineStage
from lumia.memory import Lesson
from lumia.reporting import growth_review
from lumia.seed import seed_demo_data
from lumia.tools import Toolbox


# --- CRM ------------------------------------------------------------------


def test_read_after_write(ws):
    account = Account(name="Read After Write", source="test")
    ws.crm.upsert_account(account.to_dict())
    assert ws.crm.get_account(account.id)["name"] == "Read After Write"


def test_pipeline_flags_unmanaged_and_overdue(ws):
    managed = Account(name="Managed", stage=PipelineStage.ENGAGED, next_action="Call", next_action_date="2099-01-01")
    unmanaged = Account(name="Unmanaged", stage=PipelineStage.ENGAGED)
    overdue = Account(name="Overdue", stage=PipelineStage.CONTACTED, next_action="Call", next_action_date="2000-01-01")
    for account in (managed, unmanaged, overdue):
        ws.crm.upsert_account(account.to_dict())

    report = ws.crm.pipeline()
    assert [a["name"] for a in report["unmanaged_accounts"]] == ["Unmanaged"]
    assert [a["name"] for a in report["overdue_actions"]] == ["Overdue"]


def test_unmanaged_flag_is_recomputed_not_stale(ws):
    """Regression: `unmanaged` used to be persisted at creation and never refreshed,
    so an account stayed flagged after it was given a next action."""
    account = Account(name="Stale Flag Co", stage=PipelineStage.ENGAGED)
    ws.crm.upsert_account(account.to_dict())
    assert ws.crm.get_account(account.id)["unmanaged"] is True

    ws.crm.set_next_action(account.id, "Call Thursday about the fit-out", "2099-01-01")
    assert ws.crm.get_account(account.id)["unmanaged"] is False
    # And the derived value is not written back into storage.
    assert "unmanaged" not in ws.store.get("accounts", account.id)


def test_weighted_value_tracks_probability_changes(ws):
    from lumia.domain.accounts import Opportunity

    account = Account(name="Weighted Co")
    ws.crm.upsert_account(account.to_dict())
    opportunity = Opportunity(account_id=account.id, description="Repaint", estimated_value=100_000, probability=0.2)
    ws.crm.upsert_opportunity(opportunity.to_dict())
    assert ws.crm.opportunities_for(account.id)[0]["weighted_value"] == 20_000

    opportunity.probability = 0.6
    ws.crm.upsert_opportunity(opportunity.to_dict())
    assert ws.crm.opportunities_for(account.id)[0]["weighted_value"] == 60_000


def test_logging_an_interaction_updates_last_contact(ws):
    account = Account(name="Contacted Co")
    ws.crm.upsert_account(account.to_dict())
    ws.crm.log_interaction(
        Interaction(
            account_id=account.id,
            channel=Channel.EMAIL,
            direction="outbound",
            summary="Intro",
            occurred_on="2026-05-01",
        ).to_dict()
    )
    assert ws.crm.get_account(account.id)["last_interaction_date"] == "2026-05-01"


# --- memory ---------------------------------------------------------------


def test_repeated_lessons_merge_and_gain_strength(ws):
    for _ in range(9):
        ws.memory.record_lesson(
            Lesson(
                observation="Short emails got replies",
                hypothesis="Brevity increases reply rate for estimators",
                evidence="observed again",
                segment="general_contractor",
                channel="email",
            )
        )
    lessons = ws.store.list("lessons")
    assert len(lessons) == 1
    assert lessons[0]["sample_size"] == 9
    assert lessons[0]["strength"] == "medium"


def test_recall_prefers_matching_segment(ws):
    ws.memory.record_lesson(
        Lesson(observation="A", hypothesis="pm downtime messaging works", evidence="e", segment="property_management")
    )
    ws.memory.record_lesson(
        Lesson(observation="B", hypothesis="gc bid list messaging works", evidence="e", segment="general_contractor")
    )
    top = ws.memory.recall(segment="general_contractor", limit=1)
    assert top[0]["segment"] == "general_contractor"


def test_are_record_opens_and_closes(ws):
    tools = Toolbox(ws)
    opened = tools.call(
        "open_are_record",
        {
            "action": "Email estimator",
            "objective": "Get on the bid list",
            "target": "Acme GC",
            "expected_result": "Reply within a week",
            "measurement": "Reply received",
        },
    )
    assert len(ws.memory.open_are_records()) == 1

    tools.call(
        "close_are_record",
        {"are_id": opened["id"], "result": "Replied in 2 days", "evaluation": "Timing was right"},
    )
    assert ws.memory.open_are_records() == []


# --- tools ----------------------------------------------------------------


def test_unknown_tool_returns_an_error(ws):
    assert "error" in Toolbox(ws).call("does_not_exist", {})


def test_tool_errors_are_returned_not_raised(ws):
    result = Toolbox(ws).call("get_account", {"account_id": "acct_missing"})
    assert "error" in result


def test_signal_without_recommended_action_is_rejected(ws):
    result = Toolbox(ws).call(
        "record_signal",
        {"headline": "New build", "signal_type": "permit", "source": "test", "recommended_action": "   "},
    )
    assert "error" in result


def test_next_action_cannot_be_blank(ws):
    account = Account(name="X")
    ws.crm.upsert_account(account.to_dict())
    assert "error" in Toolbox(ws).call("set_next_action", {"account_id": account.id, "action": ""})


def test_contact_requires_an_existing_account(ws):
    result = Toolbox(ws).call("upsert_contact", {"account_id": "acct_nope", "name": "Someone"})
    assert "error" in result


def test_upsert_account_does_not_duplicate_by_name(ws):
    tools = Toolbox(ws)
    first = tools.call("upsert_account", {"name": "Duplicate Co"})
    second = tools.call("upsert_account", {"name": "duplicate co", "location": "Calgary"})
    assert first["id"] == second["id"]
    assert second["_was_update"] is True


def test_estimate_is_labelled_as_not_a_quote(ws):
    result = Toolbox(ws).call(
        "estimate_paint_job",
        {"job_type": "interior", "surfaces": [{"name": "Walls", "square_feet": 3000}]},
    )
    assert result["total"] > 0
    assert "not a quote" in result["disclaimer"].lower()


def test_unconfigured_research_source_is_flagged(ws):
    result = Toolbox(ws).call("search_market_signals", {"query": "office fit-out"})
    assert result["sources_connected"] is False
    assert "UNKNOWN" in result["guidance"]


def test_relationship_history_marks_cold_accounts(ws):
    account = Account(name="Cold Co")
    ws.crm.upsert_account(account.to_dict())
    history = Toolbox(ws).call("get_relationship_history", {"account_id": account.id})
    assert history["is_cold"] is True
    assert "cold outreach" in history["guidance"]


def test_email_refuses_without_a_sender_address(ws):
    object.__setattr__(ws.settings, "company_email", "")
    account = Account(name="X")
    ws.crm.upsert_account(account.to_dict())
    result = Toolbox(ws).call(
        "send_followup_email",
        {"account_id": account.id, "to": "a@example.com", "subject": "s", "body": "b"},
    )
    assert "error" in result


# --- reporting ------------------------------------------------------------


def test_rates_are_null_when_nothing_was_sent(ws):
    review = growth_review(ws)
    assert review["conversion"]["reply_rate"] is None


def test_seed_data_is_idempotent_and_tagged(ws):
    seed_demo_data(ws)
    first = len(ws.crm.list_accounts())
    seed_demo_data(ws)
    assert len(ws.crm.list_accounts()) == first
    assert all(a["source"] == "demo_seed" for a in ws.crm.list_accounts())


def test_seed_data_produces_a_working_pipeline(ws):
    result = seed_demo_data(ws)
    assert result["pipeline"]["total_accounts"] == 3
    # The Bowmont record is deliberately left without a next action.
    assert len(result["pipeline"]["unmanaged_accounts"]) == 1
