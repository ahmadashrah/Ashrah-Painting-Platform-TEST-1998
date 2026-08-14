"""L5: many runtimes, one execution fabric."""

from __future__ import annotations

import threading
import time

import pytest
from conftest import PhaseClient, text

from lumia.contract import ContractBreach, current_contract
from lumia.fleet import (
    ALL,
    PAUSE,
    RESUME,
    TERMINATE,
    CrossRuntimeGuard,
    Fleet,
    RuntimeEntry,
    RuntimeRegistry,
)
from lumia.observability import HOOKS, RUN_PAUSED, RUN_RESUMED
from lumia.runner import Runner


@pytest.fixture(autouse=True)
def _clean_hooks():
    HOOKS.clear()
    yield
    HOOKS.clear()


@pytest.fixture
def fleet(tmp_path):
    return Fleet(tmp_path)


@pytest.fixture
def runner(settings, tmp_path):
    return Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: PhaseClient({}))


# --- 1. shared runtime registry ---------------------------------------------


def test_a_run_is_visible_to_the_fleet_while_it_runs(settings, tmp_path):
    fleet = Fleet(tmp_path)
    seen: list[list[dict]] = []

    class Watching:
        def create(self, **_kwargs):
            seen.append(fleet.registry.active())
            return text("done")

    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: Watching())
    record = runner.run("intake", "process reports")

    live = [entry for snapshot in seen for entry in snapshot]
    assert live, "the run was invisible to the fleet while running"
    assert live[0]["run"] == record.reference
    assert live[0]["role"] == "intake"
    assert live[0]["node"]
    # And it leaves no entry behind.
    assert fleet.registry.active() == []


def test_the_registry_reports_the_phase_a_run_is_in(settings, tmp_path):
    fleet = Fleet(tmp_path)
    phases: list[str] = []

    class Watching:
        def create(self, **_kwargs):
            phases.extend(e["phase"] for e in fleet.registry.active())
            return text("done")

    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: Watching())
    runner.run("client_comms", "send the log")

    assert phases[:3] == ["gather", "compose", "review"]


def test_a_dead_runtime_stops_blocking_the_fleet(fleet):
    """A crashed process must not hold a barrier open forever."""
    entry = RuntimeEntry(run="RUN-000009", role="intake", node="node-b", phase="gather")
    entry.heartbeat = time.time() - 10_000
    fleet.registry.register(entry)

    assert fleet.registry.active() == []
    assert len(fleet.registry.active(include_stale=True)) == 1
    assert fleet.registry.sweep() == ["RUN-000009"]


def test_the_registry_counts_by_node_and_phase(fleet):
    fleet.registry.register(RuntimeEntry(run="RUN-1", role="intake", node="a", phase="gather"))
    fleet.registry.register(RuntimeEntry(run="RUN-2", role="client_comms", node="b", phase="send"))

    status = fleet.status()
    assert status["active_runs"] == 2
    assert status["by_node"] == {"a": 1, "b": 1}
    assert status["by_phase"] == {"gather": 1, "send": 1}


# --- 2. distributed lifecycle commands -----------------------------------------


def test_terminate_broadcasts_to_every_runtime(runner):
    runner.fleet.lifecycle.broadcast(TERMINATE, ALL, reason="stop the fleet")
    record = runner.run("client_comms", "anything")

    assert record.killed is True
    assert "stop the fleet" in record.reply
    runner.fleet.lifecycle.clear(ALL)


def test_a_pause_holds_a_run_and_resume_releases_it(settings, tmp_path):
    """A pause is not a stop: the run keeps its place and continues."""
    fleet = Fleet(tmp_path)

    class Ticking:
        def create(self, **_kwargs):
            time.sleep(0.05)
            return text("ok")

    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: Ticking())

    def operator():
        time.sleep(0.15)
        fleet.lifecycle.broadcast(PAUSE, reason="stand by")
        time.sleep(0.5)
        fleet.lifecycle.broadcast(RESUME)

    events: list[str] = []
    HOOKS.subscribe(lambda e: events.append(e.kind), name="operator")
    threading.Thread(target=operator, daemon=True).start()

    started = time.monotonic()
    record = runner.run("intake", "work")
    took = time.monotonic() - started

    assert record.status == "finished"
    assert record.killed is False, "a pause must not kill the run"
    assert took > 0.4, "the run did not actually hold"
    assert RUN_PAUSED in events and RUN_RESUMED in events


def test_resuming_when_nothing_is_paused_is_harmless(fleet):
    result = fleet.lifecycle.broadcast(RESUME, ALL)
    assert result["released"] is False


def test_an_unknown_lifecycle_command_is_rejected(fleet):
    with pytest.raises(ValueError):
        fleet.lifecycle.broadcast("self_destruct", ALL)


# --- 3. consistent contract distribution ----------------------------------------


def test_the_contract_version_is_a_fingerprint_of_the_rules(settings):
    first = current_contract(settings)
    second = current_contract(settings)
    assert first.fingerprint() == second.fingerprint()


def test_changing_a_rule_changes_the_version(settings):
    from lumia.autonomy import TOOL_LEVELS, AutonomyLevel

    before = current_contract(settings).fingerprint()
    original = TOOL_LEVELS["send_communication"]
    TOOL_LEVELS["send_communication"] = AutonomyLevel.AUTONOMOUS
    try:
        after = current_contract(settings).fingerprint()
    finally:
        TOOL_LEVELS["send_communication"] = original

    assert before != after, "a changed rule produced the same contract version"


def test_a_node_matching_the_fleet_verifies_clean(fleet, settings):
    contract = current_contract(settings)
    assert fleet.contracts.verify(contract)["status"] == "unpublished"

    fleet.contracts.publish(contract)
    assert fleet.contracts.verify(contract)["status"] == "match"


def test_a_node_enforcing_different_rules_refuses_to_run(settings, tmp_path):
    """A stale deploy running its own rules is what this exists to catch."""
    from lumia.autonomy import TOOL_LEVELS, AutonomyLevel

    fleet = Fleet(tmp_path)
    fleet.contracts.publish(current_contract(settings))

    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: PhaseClient({}))
    original = TOOL_LEVELS["send_communication"]
    TOOL_LEVELS["send_communication"] = AutonomyLevel.AUTONOMOUS
    try:
        record = runner.run("client_comms", "send the log")
    finally:
        TOOL_LEVELS["send_communication"] = original

    assert record.status == "failed"
    assert record.stopped_because == "contract_drift"
    assert record.breached is True
    assert "the fleet is on" in record.error


def test_contract_versions_are_kept(fleet, settings):
    contract = current_contract(settings)
    published = fleet.contracts.publish(contract)

    assert fleet.contracts.versions("lumia") == [published["version"]]
    assert fleet.contracts.fetch("lumia", published["version"])["version"] == published["version"]


def test_a_run_records_the_contract_it_ran_under(runner, settings):
    runner.fleet.contracts.publish(current_contract(settings))
    record = runner.run("intake", "process reports")

    assert record.contract_version
    assert record.node
    assert runner.get(record.reference)["contract_version"] == record.contract_version


# --- 4. cross-runtime containment ------------------------------------------------


def test_runtimes_may_not_call_each_other_by_default():
    guard = CrossRuntimeGuard()
    assert guard.permitted("node-b") is False

    with pytest.raises(ContractBreach) as excinfo:
        guard.check("node-b")
    assert "no containment" in excinfo.value.breach.detail


def test_approved_runtimes_can_be_named(monkeypatch):
    monkeypatch.setenv("LUMIA_APPROVED_RUNTIMES", "node-b, node-c")
    guard = CrossRuntimeGuard.from_env()

    assert guard.permitted("node-b")
    assert guard.permitted("node-c")
    assert not guard.permitted("node-d")


# --- 5. coordinated phase transitions ---------------------------------------------


def test_a_phase_waits_for_the_cohort(fleet):
    barrier = fleet.barrier(["gather", "compose", "review", "send", "record"])
    fleet.registry.register(RuntimeEntry(run="RUN-PEER", role="intake", node="b", phase="gather"))
    fleet.registry.update("RUN-PEER", cohort="riverbend")

    # A peer still gathering blocks anyone trying to send.
    assert barrier.ready("riverbend", "send") is False
    assert barrier.blockers("riverbend", "send")[0]["run"] == "RUN-PEER"

    fleet.registry.update("RUN-PEER", phase="record")
    assert barrier.ready("riverbend", "send") is True


def test_a_barrier_only_holds_its_own_cohort(fleet):
    barrier = fleet.barrier(["gather", "send"])
    fleet.registry.register(RuntimeEntry(run="RUN-OTHER", role="intake", node="b", phase="gather"))
    fleet.registry.update("RUN-OTHER", cohort="a-different-project")

    assert barrier.ready("riverbend", "send") is True


def test_a_barrier_times_out_rather_than_stalling_the_fleet(fleet):
    """One wedged runtime must not stop everyone else working."""
    barrier = fleet.barrier(["gather", "send"])
    fleet.registry.register(RuntimeEntry(run="RUN-STUCK", role="intake", node="b", phase="gather"))
    fleet.registry.update("RUN-STUCK", cohort="riverbend")

    result = barrier.wait("riverbend", "send", timeout=0.3, poll=0.05)

    assert result["ready"] is False
    assert result["timed_out"] is True
    assert result["waiting_on"][0]["run"] == "RUN-STUCK"


def test_a_paused_peer_does_not_block_the_fleet(fleet):
    barrier = fleet.barrier(["gather", "send"])
    fleet.registry.register(RuntimeEntry(run="RUN-HELD", role="intake", node="b", phase="gather"))
    fleet.registry.update("RUN-HELD", cohort="riverbend", status="paused")

    assert barrier.ready("riverbend", "send") is True


def test_a_run_without_a_cohort_never_waits(runner):
    """Most work is not fleet-coordinated and must not pay for the option."""
    record = runner.run("intake", "process reports")
    assert record.cohort == ""
    assert record.status == "finished"
