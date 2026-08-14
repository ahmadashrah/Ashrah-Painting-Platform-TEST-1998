"""Routing, role toolsets and prompt selection for the communication side."""

from __future__ import annotations

import pytest
from conftest import FakeClient, text

from lumia.agents import COMMS_ROLES, GROWTH_ROLES, ROLE_TOOLS, build_agent
from lumia.comms.desk import CommunicationDesk
from lumia.comms.prompts import CORE as COMMS_CORE
from lumia.prompts import CORE as GROWTH_CORE
from lumia.prompts import system_prompt
from lumia.tools import Toolbox


@pytest.fixture
def desk(ws):
    return CommunicationDesk(workspace=ws, toolbox=Toolbox(ws), client=FakeClient([text("done")]))


@pytest.mark.parametrize(
    "task, expected",
    [
        ("translate the voice note the crew lead sent", "intake"),
        ("send the daily log to the general contractor", "client_comms"),
        ("tell the crew what time to start tomorrow", "crew_comms"),
        ("order more primer from the supplier", "vendor_comms"),
        ("the client is threatening to escalate this delay", "escalation"),
    ],
)
def test_requests_route_to_the_right_specialist(desk, task, expected):
    assert desk.route(task) == expected


def test_unmatched_requests_default_to_client_reporting(desk):
    assert desk.route("something entirely unrelated") == "client_comms"


def test_an_unknown_role_is_rejected(desk):
    with pytest.raises(ValueError):
        desk.handle("do something", role="marketing_wizard")


def test_growth_roles_cannot_reach_communication_tools(ws):
    """The Outreach agent has no business sending a client daily log."""
    outreach = build_agent("outreach", ws, Toolbox(ws))
    assert "send_communication" not in outreach.allowed_tools
    assert "compose_daily_log" not in outreach.allowed_tools


def test_communication_roles_cannot_reach_growth_outbound(ws):
    for role in COMMS_ROLES:
        agent = build_agent(role, ws, Toolbox(ws))
        assert "send_first_contact_email" not in agent.allowed_tools
        assert "publish_content" not in agent.allowed_tools


def test_only_the_supplier_role_can_order_materials(ws):
    holders = [role for role in COMMS_ROLES if "place_material_order" in ROLE_TOOLS[role]]
    assert holders == ["vendor_comms"]


def test_the_client_agent_cannot_dispatch_crew_records(ws):
    client = build_agent("client_comms", ws, Toolbox(ws))
    assert "upsert_crew_member" not in client.allowed_tools


def test_every_role_can_escalate(ws):
    for role in COMMS_ROLES:
        assert "raise_escalation" in build_agent(role, ws, Toolbox(ws)).allowed_tools


def test_every_role_tool_actually_exists(ws):
    box = Toolbox(ws)
    for role, names in ROLE_TOOLS.items():
        missing = [n for n in names if not box.has(n)]
        assert missing == [], f"{role} lists tools that do not exist: {missing}"


def test_each_role_family_gets_its_own_operating_spec(settings):
    for role in COMMS_ROLES:
        prompt = system_prompt(role, settings)
        assert prompt.startswith(COMMS_CORE[:80])
        assert "Lumia Communication Agent" in prompt

    for role in GROWTH_ROLES:
        prompt = system_prompt(role, settings)
        assert prompt.startswith(GROWTH_CORE[:80])
        assert "Lumia Marketing" in prompt


def test_the_role_addendum_is_included(settings):
    assert "Field Intake" in system_prompt("intake", settings)
    assert "Client Reporting" in system_prompt("client_comms", settings)


def test_the_prompt_names_which_integrations_are_simulated(settings):
    prompt = system_prompt("client_comms", settings)
    assert "Unconfigured integrations" in prompt
    assert "email" in prompt
