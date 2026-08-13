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


def test_a_tool_outside_the_phase_is_refused_not_run(settings, tmp_path):
    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: PhaseClient({
        "gather": [calls_tool("send_communication", {"draft_id": "comm_x"}), text("gathered")],
    }))
    events: list[Event] = []
    HOOKS.subscribe(events.append, name="operator")

    record = runner.run("client_comms", "send the log")

    refused = [e for e in events if e.kind == TOOL_REFUSED]
    assert len(refused) == 1
    assert refused[0].detail["tool"] == "send_communication"
    assert refused[0].detail["phase"] == "gather"
    assert "send_communication" not in record.tools_executed


def test_the_refusal_tells_the_model_what_it_may_use(settings, tmp_path):
    """A refusal that does not say what is available just gets retried."""
    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: PhaseClient({
        "gather": [calls_tool("send_communication", {"draft_id": "comm_x"}), text("ok")],
    }))
    record = runner.run("client_comms", "send the log")

    gather = next(p for p in record.phases if p["phase"] == "gather")
    assert "send_communication" in gather["tools_refused"]
    assert "build_daily_log" in gather["available_tools"]


def test_enforcement_does_not_depend_on_the_schema_list(ws):
    """Omitting a schema makes a call unlikely; refusing makes it impossible."""
    from lumia.agent import Agent

    agent = Agent(role="client_comms", workspace=ws, toolbox=Toolbox(ws),
                  client=FakeClient([calls_tool("send_communication", {"draft_id": "x"}), text("done")]),
                  allowed_tools=["build_daily_log"])
    run = agent.run("try to send anyway")

    assert run.tool_calls[0].executed is False
    assert "outside this phase" in run.tool_calls[0].result_summary


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
