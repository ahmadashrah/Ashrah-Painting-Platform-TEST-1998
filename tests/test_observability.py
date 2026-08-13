"""The operator's window, and the ability to stop a run."""

from __future__ import annotations

import json
import threading
import time

import pytest
from conftest import FakeClient, PhaseClient, calls_tool, text

from lumia.observability import (
    GATE_DECIDED,
    HOOKS,
    MODEL_REPLIED,
    RUN_FINISHED,
    RUN_KILLED,
    RUN_STARTED,
    TOOL_EXECUTED,
    TOOL_GATED,
    TOOL_PROPOSED,
    TURN_STARTED,
    Event,
    HookRegistry,
    JsonlRecorder,
    KillSwitch,
    RunObserver,
)
from lumia.runner import Runner


@pytest.fixture(autouse=True)
def _clean_hooks():
    """Hooks are global by design; a test must not leak into the next."""
    HOOKS.clear()
    yield
    HOOKS.clear()


@pytest.fixture
def runner(settings, tmp_path):
    return Runner(settings=settings, data_dir=tmp_path,
                  client_factory=lambda s: FakeClient([calls_tool("list_projects", {}), text("done")]))


# --- an operator sees every step -------------------------------------------


def test_a_hook_sees_the_whole_run(runner):
    seen: list[str] = []
    HOOKS.subscribe(lambda e: seen.append(e.kind), name="operator")

    runner.run("client_comms", "list the projects")

    # The shape of a healthy run, in order.
    assert seen[0] == RUN_STARTED
    assert seen[-1] == RUN_FINISHED
    for kind in (TURN_STARTED, MODEL_REPLIED, TOOL_PROPOSED, GATE_DECIDED, TOOL_EXECUTED):
        assert kind in seen, f"an operator could not see {kind}"


def test_the_gate_decision_is_visible_with_its_reason(settings, tmp_path):
    """The most important thing to watch: what was allowed, and why."""
    from lumia.comms.seed import seed_demo_projects

    runner = Runner(settings=settings, data_dir=tmp_path)
    workspace, _ = runner.build()
    project = seed_demo_projects(workspace)["project_id"]
    # Ordering belongs to the prepare phase.
    runner.client_factory = lambda s: PhaseClient({"prepare": [
        calls_tool("place_material_order", {
            "project_id": project, "supplier": "Priya Anand (DEMO)",
            "items": ["primer"], "delivery_location": "bay"}),
        text("Needs your approval."),
    ]})

    events: list[Event] = []
    HOOKS.subscribe(events.append, name="operator")
    record = runner.run("vendor_comms", "order primer")

    decision = next(e for e in events if e.kind == GATE_DECIDED)
    assert decision.detail["level"] == 3
    assert "place_material_order" in decision.detail["tool"]

    gated = next(e for e in events if e.kind == TOOL_GATED)
    assert gated.detail["approval_id"] == record.approvals_raised[0]


def test_steps_are_numbered_and_carry_the_run(runner):
    record = runner.run("client_comms", "list the projects")
    steps = runner.steps(record.reference)

    assert [s["seq"] for s in steps] == list(range(1, len(steps) + 1))
    assert {s["run"] for s in steps} == {record.reference}
    assert all(s["elapsed_seconds"] >= 0 for s in steps)


def test_steps_of_one_run_are_not_mixed_with_another(runner):
    first = runner.run("client_comms", "one")
    second = runner.run("intake", "two")

    assert {s["run"] for s in runner.steps(first.reference)} == {first.reference}
    assert {s["run"] for s in runner.steps(second.reference)} == {second.reference}


def test_a_broken_hook_cannot_take_down_a_run(runner):
    """An operator's broken dashboard must not stop the crew dispatch."""
    def explodes(_event):
        raise RuntimeError("the dashboard is down")

    survived: list[str] = []
    HOOKS.subscribe(explodes, name="broken")
    HOOKS.subscribe(lambda e: survived.append(e.kind), name="good")

    record = runner.run("client_comms", "list the projects")

    assert record.status == "finished"
    assert survived, "a later hook was skipped because an earlier one failed"


def test_a_hook_can_be_removed_again(runner):
    seen: list[str] = []
    unsubscribe = HOOKS.subscribe(seen.append, name="temporary")
    assert "temporary" in HOOKS.names

    unsubscribe()
    runner.run("client_comms", "one")

    assert seen == []
    assert "temporary" not in HOOKS.names


def test_long_payloads_are_trimmed_before_recording():
    registry = HookRegistry()
    captured: list[Event] = []
    registry.subscribe(captured.append)

    RunObserver("RUN-000001", "intake", registry).emit("tool.executed", result="x" * 5000)

    assert len(captured[0].detail["result"]) < 2000
    assert "+3800 chars" in captured[0].detail["result"]


def test_the_recorder_survives_a_torn_line(tmp_path):
    path = tmp_path / "events.jsonl"
    recorder = JsonlRecorder(path)
    recorder(Event(run="RUN-000001", kind="run.started"))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"run": "RUN-000001", "kind": incomplete\n')
    recorder(Event(run="RUN-000001", kind="run.finished"))

    kinds = [e["kind"] for e in recorder.read()]
    assert kinds == ["run.started", "run.finished"]


def test_a_budget_warning_fires_before_a_run_is_late():
    from lumia.deadline import Deadline

    registry = HookRegistry()
    seen: list[Event] = []
    registry.subscribe(seen.append)
    observer = RunObserver("RUN-000001", "client_comms", registry)

    deadline = Deadline(budget_seconds=100.0)
    observer.budget_check(deadline)
    assert seen == []                       # nothing spent yet

    # Wind the clock back so the budget reads as nearly spent.
    deadline.started -= 90.0
    observer.budget_check(deadline)
    observer.budget_check(deadline)         # warns once, not every turn

    warnings = [e for e in seen if e.kind == "budget.warning"]
    assert len(warnings) == 1


# --- stopping a run that is already going -----------------------------------


def test_an_operator_can_kill_a_running_run(settings, tmp_path):
    class Endless:
        def create(self, **_kwargs):
            time.sleep(0.05)
            return calls_tool("list_projects", {}, use_id=f"t{time.time()}")

    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: Endless())

    def operator():
        time.sleep(0.3)
        runner.kill_switch.request("RUN-000001", reason="wrong project")

    threading.Thread(target=operator, daemon=True).start()
    record = runner.run("client_comms", "keep going forever", budget_seconds=60.0)

    assert record.killed is True
    assert record.stopped_because == "killed"
    assert "wrong project" in record.reply
    assert "did not happen" in record.reply


def test_killing_all_stops_the_next_run_too(runner):
    runner.kill_switch.request(KillSwitch.ALL, reason="stop the world")
    record = runner.run("client_comms", "anything")

    assert record.killed is True
    assert "stop the world" in record.reply
    runner.kill_switch.release_all()


def test_a_kill_targets_one_run_and_never_the_next(runner):
    """References are never reused, so a request means exactly one run."""
    runner.kill_switch.request("RUN-000001", reason="stop this one")

    first = runner.run("client_comms", "one")
    assert first.killed is True, "a kill armed before the run started was ignored"

    second = runner.run("client_comms", "two")
    assert second.killed is False, "the request leaked into an unrelated run"
    assert runner.kill_switch.pending() == []


def test_a_kill_is_visible_in_the_step_log(settings, tmp_path):
    runner = Runner(settings=settings, data_dir=tmp_path,
                    client_factory=lambda s: FakeClient([text("ok")]))
    runner.kill_switch.request(KillSwitch.ALL, reason="operator stop")
    record = runner.run("intake", "anything")
    runner.kill_switch.release_all()

    kinds = [e["kind"] for e in runner.steps(record.reference)]
    assert RUN_KILLED in kinds


def test_the_kill_switch_reports_what_is_pending(tmp_path):
    switch = KillSwitch(tmp_path / "kill")
    assert switch.pending() == []

    switch.request("RUN-000007", reason="testing")
    assert switch.pending() == ["RUN-000007"]
    assert switch.requested("RUN-000007")["reason"] == "testing"
    assert switch.requested("RUN-000008") is None

    switch.clear("RUN-000007")
    assert switch.pending() == []


def test_a_reference_cannot_escape_the_kill_directory(tmp_path):
    """A reference arrives from an operator; it must not be a path."""
    switch = KillSwitch(tmp_path / "kill")
    switch.request("../../etc/passwd", reason="nice try")

    assert not (tmp_path.parent / "etc").exists()
    assert switch.pending() == ["etcpasswd"]


# --- the hard stop ------------------------------------------------------------


def test_a_wedged_run_is_interrupted_even_though_nothing_is_checking(settings, tmp_path):
    """The cooperative budget cannot help if the block is below it."""
    import lumia.runner as runner_module

    class Wedged:
        def create(self, **_kwargs):
            time.sleep(30)

    original = runner_module.WATCHDOG_GRACE_SECONDS
    runner_module.WATCHDOG_GRACE_SECONDS = 1.0
    try:
        runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: Wedged())
        started = time.monotonic()
        record = runner.run("client_comms", "wedge", budget_seconds=2.0)
        took = time.monotonic() - started
    finally:
        runner_module.WATCHDOG_GRACE_SECONDS = original

    assert took < 10, "the watchdog did not fire on a blocked call"
    assert record.timed_out is True
    assert "stopped responding to its own time budget" in record.error
    # Still numbered, still findable.
    assert runner.get(record.reference)["timed_out"] is True


def test_the_watchdog_leaves_no_timer_behind(settings, tmp_path):
    """A leaked alarm would fire during unrelated work later."""
    import signal

    runner = Runner(settings=settings, data_dir=tmp_path,
                    client_factory=lambda s: FakeClient([text("ok")]))
    runner.run("intake", "quick job")

    remaining, _interval = signal.getitimer(signal.ITIMER_REAL)
    assert remaining == 0.0
