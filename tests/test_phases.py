"""Phases: ordered, logged, and each holding only its own tools."""

from __future__ import annotations

import pytest
from conftest import FakeClient, PhaseClient, calls_tool, text

from lumia.agents import ROLE_TOOLS
from lumia.observability import HOOKS, PHASE_FINISHED, PHASE_STARTED, TOOL_REFUSED, Event
from lumia.phases import PLANS, Phase, plan_for
from lumia.runner import Runner
from lumia.tools import Toolbox


@pytest.fixture(autouse=True)
def _clean_hooks():
    HOOKS.clear()
    yield
    HOOKS.clear()


@pytest.fixture
def runner(settings, tmp_path):
    return Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: PhaseClient({}))


# --- the plans themselves ---------------------------------------------------


def test_every_agent_has_phases(ws):
    box = Toolbox(ws)
    for role in ROLE_TOOLS:
        phases = plan_for(role, ROLE_TOOLS[role])
        assert phases, f"{role} has no phases"
        assert len(phases) >= 2 or role not in PLANS


def test_every_phase_names_real_tools(ws):
    """A phase listing a tool that does not exist would silently do nothing."""
    box = Toolbox(ws)
    for role, phases in PLANS.items():
        for phase in phases:
            missing = [t for t in phase.tools if not box.has(t)]
            assert missing == [], f"{role}/{phase.name} lists tools that do not exist: {missing}"


def test_a_plan_can_narrow_a_role_but_never_widen_it():
    """A phase naming a tool the role lacks must not grant it."""
    narrowed = plan_for("client_comms", allowed=["build_daily_log", "get_project"])
    for phase in narrowed:
        assert set(phase.tools) <= {"build_daily_log", "get_project"}


def test_a_role_without_a_plan_still_runs():
    phases = plan_for("some_future_agent", allowed=["list_projects"])
    assert len(phases) == 1
    assert phases[0].name == "execute"
    assert phases[0].tools == ["list_projects"]


def test_gathering_phases_hold_no_sending_tools():
    """The point of the split: reading cannot become sending."""
    sending = {"send_communication", "place_material_order", "send_first_contact_email",
               "publish_content", "send_followup_email"}
    for role, phases in PLANS.items():
        first = phases[0]
        assert not (set(first.tools) & sending), (
            f"{role}'s first phase ({first.name}) can already send"
        )


# --- enforcement ------------------------------------------------------------


def test_a_tool_outside_the_phase_kills_the_run(settings, tmp_path):
    """Reaching past the phase is a contract breach, not a wrong guess."""
    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: PhaseClient({
        "gather": [calls_tool("send_communication", {"draft_id": "comm_x"}), text("gathered")],
    }))
    events: list[Event] = []
    HOOKS.subscribe(events.append, name="operator")

    record = runner.run("client_comms", "send the log")

    assert record.breached is True
    assert record.killed is True
    assert record.stopped_because == "contract_breach"
    assert record.breach["attempted"] == "send_communication"
    assert record.breach["phase"] == "gather"
    assert "send_communication" not in record.tools_executed

    # And the run stopped there — no later phase ran.
    assert [p["phase"] for p in record.phases] == []
    kinds = [e.kind for e in events]
    assert "contract.breached" in kinds
    # And the run closes as killed, never as a clean finish.
    assert kinds[-1] == "run.killed"


def test_the_breach_records_what_was_available(settings, tmp_path):
    """An operator has to be able to see what the run should have used."""
    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: PhaseClient({
        "gather": [calls_tool("send_communication", {"draft_id": "comm_x"}), text("ok")],
    }))
    record = runner.run("client_comms", "send the log")

    assert "build_daily_log" in record.breach["allowed"]
    assert "send_communication" in record.reply


def test_an_unknown_tool_is_a_breach_too(settings, tmp_path):
    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: PhaseClient({
        "gather": [calls_tool("wire_funds_somewhere", {}), text("ok")],
    }))
    record = runner.run("client_comms", "do something odd")

    assert record.breached is True
    assert record.breach["kind"] == "unknown_tool"


def test_enforcement_does_not_depend_on_the_schema_list(ws):
    """Omitting a schema makes a call unlikely; stopping makes it impossible."""
    from lumia.agent import Agent
    from lumia.contract import ContractBreach

    agent = Agent(role="client_comms", workspace=ws, toolbox=Toolbox(ws),
                  client=FakeClient([calls_tool("send_communication", {"draft_id": "x"}), text("done")]),
                  allowed_tools=["build_daily_log"])

    with pytest.raises(ContractBreach) as excinfo:
        agent.run("try to send anyway")
    assert excinfo.value.breach.attempted == "send_communication"


def test_the_policy_can_be_softened_to_refuse(ws, monkeypatch):
    """Killing is the default; some deployments want the agent to recover."""
    from lumia.agent import Agent

    monkeypatch.setenv("LUMIA_ON_BREACH", "refuse")

    agent = Agent(role="client_comms", workspace=ws, toolbox=Toolbox(ws),
                  client=FakeClient([calls_tool("send_communication", {"draft_id": "x"}), text("done")]),
                  allowed_tools=["build_daily_log"])
    run = agent.run("try to send anyway")

    assert run.tool_calls[0].executed is False
    assert "outside this phase" in run.tool_calls[0].result_summary


def test_being_gated_is_never_a_breach(settings, tmp_path):
    """A Level 3 tool queued for approval is the system working, not a bypass."""
    from lumia.comms.seed import seed_demo_projects

    runner = Runner(settings=settings, data_dir=tmp_path)
    workspace, _ = runner.build()
    project = seed_demo_projects(workspace)["project_id"]
    runner.client_factory = lambda s: PhaseClient({"prepare": [
        calls_tool("place_material_order", {
            "project_id": project, "supplier": "Priya Anand (DEMO)",
            "items": ["primer"], "delivery_location": "bay"}),
        text("Queued for approval."),
    ]})

    record = runner.run("vendor_comms", "order primer")

    assert record.breached is False
    assert record.killed is False
    assert record.approvals_raised, "the gate did not fire at all"


# --- logging -----------------------------------------------------------------


def test_each_phase_is_logged_with_what_it_used(runner):
    record = runner.run("intake", "process the reports")

    assert [p["phase"] for p in record.phases] == [p.name for p in PLANS["intake"]]
    for entry in record.phases:
        assert entry["goal"]
        assert entry["available_tools"]
        assert "tools_used" in entry
        assert entry["index"] >= 1


def test_phase_boundaries_appear_in_the_step_log(runner):
    events: list[Event] = []
    HOOKS.subscribe(events.append, name="operator")

    record = runner.run("vendor_comms", "order primer")

    started = [e for e in events if e.kind == PHASE_STARTED]
    finished = [e for e in events if e.kind == PHASE_FINISHED]
    assert [e.detail["phase"] for e in started] == [p["phase"] for p in record.phases]
    assert len(started) == len(finished)
    # An operator can see where the run is and what it may do there.
    assert started[0].detail["goal"]
    assert started[0].detail["tools"]


def test_phases_run_in_order(runner):
    record = runner.run("client_comms", "send the log")
    assert [p["index"] for p in record.phases] == list(range(1, len(record.phases) + 1))
    assert [p["phase"] for p in record.phases][:2] == ["gather", "compose"]


def test_a_phase_hands_its_findings_to_the_next(settings, tmp_path):
    client = PhaseClient({"gather": [text("Two submissions, both verified.")]})
    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: client)
    runner.run("client_comms", "send the log")

    later = [r for r in client.requests if ": compose ---" in str(r["messages"][0]["content"])]
    assert later, "the compose phase never ran"
    assert "Two submissions, both verified." in str(later[0]["messages"][0]["content"])


def test_one_run_number_covers_every_phase(runner):
    record = runner.run("intake", "process the reports")
    steps = runner.steps(record.reference)

    assert {s["run"] for s in steps} == {record.reference}
    assert len(record.phases) > 1


# --- phases and the other guarantees -----------------------------------------


def test_the_budget_is_shared_across_phases_not_per_phase(settings, tmp_path):
    """Five phases must not mean five times the time budget."""
    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: PhaseClient({}))
    record = runner.run("client_comms", "send the log", budget_seconds=0.0)

    assert record.timed_out is True
    assert record.phases == [], "a spent budget still entered a phase"
    assert "Nothing was done" in record.reply


def test_a_kill_between_phases_is_reported_as_a_kill(settings, tmp_path):
    """The flags must be set at a phase boundary, not only mid-turn."""
    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: PhaseClient({}))
    runner.kill_switch.request("ALL", reason="stop now")
    record = runner.run("client_comms", "send the log")
    runner.kill_switch.release_all()

    assert record.killed is True
    assert record.stopped_because == "killed"
    assert "stop now" in record.reply


# --- out-of-scope network calls ---------------------------------------------


def test_the_egress_guard_allows_only_configured_hosts(settings):
    from lumia.contract import ContractBreach, EgressGuard

    guard = EgressGuard.from_settings(settings)

    assert guard.permitted("https://api.anthropic.com/v1/messages")
    assert guard.permitted("https://api.openai.com/v1/chat/completions")
    assert not guard.permitted("https://attacker.example.net/collect")

    with pytest.raises(ContractBreach) as excinfo:
        guard.check("http://169.254.169.254/latest/meta-data/", what="fetching a recording")
    assert excinfo.value.breach.kind == "out_of_scope_host"
    assert "169.254.169.254" in excinfo.value.breach.detail


def test_a_lookalike_host_does_not_pass():
    """'evil-example.com' must not satisfy an allowance for 'example.com'."""
    from lumia.contract import EgressGuard

    guard = EgressGuard(hosts={"example.com"})

    assert guard.permitted("https://example.com/x")
    assert guard.permitted("https://media.example.com/x")     # a real subdomain
    assert not guard.permitted("https://evil-example.com/x")
    assert not guard.permitted("https://example.com.attacker.net/x")


def test_hosts_can_be_allowed_explicitly(monkeypatch, settings):
    from lumia.contract import EgressGuard

    monkeypatch.setenv("LUMIA_ALLOWED_HOSTS", "media.ashrah.example, cdn.ashrah.example")
    guard = EgressGuard.from_settings(settings)

    assert guard.permitted("https://media.ashrah.example/photo.jpg")
    assert guard.permitted("https://cdn.ashrah.example/a.m4a")
    assert not guard.permitted("https://elsewhere.example/a.m4a")


def test_fetching_a_recording_from_an_unlisted_host_is_a_breach(settings):
    """The agent supplies this URL. Without the guard it is fetch-anything."""
    from lumia.config import ServiceCredentials
    from lumia.contract import ContractBreach, EgressGuard
    from lumia.integrations.openai import OpenAIService

    service = OpenAIService(ServiceCredentials(name="openai", api_key="sk-test",
                                               base_url="https://api.openai.com/v1"))
    service.egress = EgressGuard.from_settings(settings)

    with pytest.raises(ContractBreach):
        service.transcribe("https://attacker.example.net/not-audio")


def test_reading_a_photo_from_an_unlisted_host_is_a_breach(settings):
    from lumia.config import ServiceCredentials
    from lumia.contract import ContractBreach, EgressGuard
    from lumia.integrations.openai import OpenAIService

    service = OpenAIService(ServiceCredentials(name="openai", api_key="sk-test",
                                               base_url="https://api.openai.com/v1"))
    service.egress = EgressGuard.from_settings(settings)

    with pytest.raises(ContractBreach):
        service.describe_image("https://attacker.example.net/photo.jpg")


def test_a_run_binds_the_guard_to_every_integration(settings, tmp_path):
    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: PhaseClient({}))
    record = runner.run("intake", "process reports")
    assert record.status == "finished"

    # The guard is attached during the run; a fresh workspace has none until one starts.
    workspace, _ = runner.build()
    workspace.set_egress_guard(object())
    for service in workspace._services():
        assert service.egress is not None
