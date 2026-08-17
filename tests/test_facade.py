"""The front door. Most of it must work with no credentials at all."""

from __future__ import annotations

import pytest

from lumia.autonomy import TOOL_LEVELS
from lumia.facade import Lumia
from lumia.llm import MissingAPIKey


@pytest.fixture
def lumia(settings, tmp_path):
    return Lumia(settings=settings, data_dir=tmp_path)


@pytest.fixture
def offline(settings, tmp_path):
    """A platform with no API key — the state a fresh clone is in."""
    return Lumia(settings=type(settings)(**{**settings.__dict__, "anthropic_api_key": None}), data_dir=tmp_path)


def test_it_builds_without_credentials(offline):
    assert offline.live is False
    assert offline.status()["can_run_agents"] is False
    assert "ANTHROPIC_API_KEY is not set" in offline.status()["blocked"]


def test_every_agent_is_described(lumia):
    agents = lumia.agents()
    assert len(agents) == 10
    assert {a.family for a in agents} == {"growth", "communication"}
    for agent in agents:
        assert agent.purpose, f"{agent.role} has no stated purpose"
        assert agent.tools


def test_agents_can_be_filtered_by_family(lumia):
    assert {a.role for a in lumia.agents(family="communication")} == {
        "intake",
        "client_comms",
        "crew_comms",
        "vendor_comms",
        "escalation",
    }


def test_agent_tools_are_split_by_what_they_cost(lumia):
    vendor = next(a for a in lumia.agents() if a.role == "vendor_comms")
    assert "place_material_order" in vendor.approval_required
    assert "draft_communication" in vendor.autonomous


def test_the_tool_listing_carries_levels_and_descriptions(lumia):
    tools = lumia.tools("client_comms")
    assert tools
    for tool in tools:
        assert tool["level"] in {1, 2, 3}
        assert tool["description"]
        assert TOOL_LEVELS[tool["name"]] == tool["level"]


def test_screening_runs_offline(offline):
    result = offline.screen("The extra ceiling work will be $4,200.")
    assert result["requires_approval"] is True
    assert "pricing" in result["categories"]


def test_the_gate_can_be_dry_run_offline(offline):
    project = offline.seed()["projects"]["project_id"]
    draft = offline.call(
        "draft_communication",
        project_id=project,
        recipient="Dana Whitfield (DEMO)",
        kind="daily_log",
        subject="Daily Project Update",
        body="Primer applied to corridor walls.",
    )

    clean = offline.gate("send_communication", draft_id=draft["id"])
    assert clean["level"] == 2

    offline.workspace.store.patch("communications", draft["id"], {"body": "That will be $900."})
    risky = offline.gate("send_communication", draft_id=draft["id"])
    assert risky["level"] == 3
    assert "NOT execute" in risky["verdict"]


def test_the_gate_flags_an_unknown_tool(lumia):
    decision = lumia.gate("wire_funds_somewhere")
    assert decision["level"] == 3
    assert decision["known_tool"] is False


def test_calling_a_level_three_tool_directly_is_refused(offline):
    project = offline.seed()["projects"]["project_id"]
    result = offline.call(
        "place_material_order",
        project_id=project,
        supplier="Priya Anand (DEMO)",
        items=["Sherwin-Williams ProMar 200, eggshell, 20L"],
        delivery_location="Level 1 loading bay",
    )
    assert "error" in result
    assert result["gate"]["level"] == 3
    # And nothing was written.
    assert offline.workspace.comms.communications_for(project) == []


def test_calling_an_allowed_tool_works(offline):
    offline.seed()
    result = offline.call("list_projects")
    assert result["count"] == 1


@pytest.mark.parametrize(
    "task, expected",
    [
        ("send the daily log to the general contractor", "client_comms"),
        ("find property managers in Calgary", "research"),
        ("order more primer from the supplier", "vendor_comms"),
        ("translate the crew voice note", "intake"),
        ("draft a case study", "content"),
    ],
)
def test_routing_spans_both_families(lumia, task, expected):
    assert lumia.route(task) == expected


def test_ties_break_toward_the_cautious_role(lumia):
    """'client' and 'escalate' match equally; burying a threat is the worse error."""
    assert lumia.route("the client is threatening to escalate") == "escalation"


def test_an_unmatched_request_goes_to_the_director(lumia):
    assert lumia.route("something entirely unrelated") == "director"


def test_asking_without_a_key_names_what_still_works(offline):
    with pytest.raises(MissingAPIKey) as excinfo:
        offline.ask("send today's daily log")
    message = str(excinfo.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "screen()" in message


def test_asking_with_an_unknown_role_is_rejected(lumia):
    with pytest.raises(ValueError):
        lumia.ask("do something", role="marketing_wizard")


def test_seed_loads_both_sides(offline):
    seeded = offline.seed()
    assert seeded["growth"]["seeded_accounts"]
    assert seeded["projects"]["project_id"]
    assert len(offline.projects()) == 1
    assert len(offline.accounts()) == 3


def test_reports_returns_both_scoreboards(offline):
    reports = offline.reports(days=7)
    assert "pipeline" in reports["growth"]
    assert "delivery" in reports["communication"]


def test_approving_a_queued_action_executes_it(offline):
    from lumia.autonomy import ApprovalRequest

    project = offline.seed()["projects"]["project_id"]
    draft = offline.call(
        "draft_communication",
        project_id=project,
        recipient="Dana Whitfield (DEMO)",
        kind="commitment",
        subject="Confirmation",
        body="Confirming the arrangement we discussed.",
    )
    request = offline.workspace.approvals.submit(
        ApprovalRequest(tool="send_communication", arguments={"draft_id": draft["id"]}, reason="commitment")
    )

    assert len(offline.approvals()) == 1
    result = offline.approve(request.id, note="ok")
    assert result["result"]["status"] == "sent"

    stored = offline.workspace.comms.get_communication(draft["id"])
    assert stored["approval_id"] == request.id
    assert stored["approval_source"] == "human_approval"
    assert offline.approvals() == []


def test_rejecting_leaves_the_action_undone(offline):
    from lumia.autonomy import ApprovalRequest

    request = offline.workspace.approvals.submit(
        ApprovalRequest(tool="send_communication", arguments={"draft_id": "comm_x"}, reason="test")
    )
    assert offline.reject(request.id, note="wrong recipient")["status"] == "rejected"
    assert offline.approvals() == []


def test_repr_says_whether_it_can_run(offline, lumia):
    assert "no API key" in repr(offline)
    assert "live" in repr(lumia)


# --- where the keys go ----------------------------------------------------


def test_the_key_report_names_the_variable_to_set(offline):
    keys = offline.keys()
    assert keys["ANTHROPIC_API_KEY"]["set"] is False
    assert keys["ANTHROPIC_API_KEY"]["required"] is True
    # Not just "email is mocked" — the exact variable.
    assert "RESEND_API_KEY" in keys
    assert keys["RESEND_API_KEY"]["unlocks"]


def test_a_multi_variable_service_lists_what_is_still_missing():
    """Twilio needs three variables; setting one is not being configured."""
    from lumia.config import ServiceCredentials

    partial = ServiceCredentials(
        name="sms",
        api_key="token",
        key_var="TWILIO_AUTH_TOKEN",
        also_needs=("TWILIO_ACCOUNT_SID", "TWILIO_FROM_NUMBER"),
    )
    assert partial.missing_vars == ["TWILIO_ACCOUNT_SID", "TWILIO_FROM_NUMBER"]

    nothing = ServiceCredentials(name="email", key_var="SENDGRID_API_KEY")
    assert nothing.missing_vars == ["SENDGRID_API_KEY"]


def test_status_carries_the_key_report(offline):
    assert "keys" in offline.status()
