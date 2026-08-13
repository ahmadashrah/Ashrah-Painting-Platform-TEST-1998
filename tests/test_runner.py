"""Isolation: agents share nothing, and no run inherits the last one."""

from __future__ import annotations

import pytest
from conftest import FakeClient, calls_tool, text

from lumia.runner import Runner


@pytest.fixture
def runner(settings, tmp_path):
    """A runner whose every run gets a fresh scripted client."""
    def factory(_settings):
        return FakeClient([calls_tool("list_projects", {}), text("done")])

    return Runner(settings=settings, data_dir=tmp_path, client_factory=factory)


# --- agents share nothing mutable -----------------------------------------


def test_each_run_builds_its_own_stack(runner):
    ws1, box1 = runner.build()
    ws2, box2 = runner.build()

    assert ws1 is not ws2
    assert box1 is not box2
    assert ws1.store is not ws2.store
    assert ws1.comms is not ws2.comms
    assert ws1.approvals is not ws2.approvals


def test_two_agents_never_share_a_toolbox(settings, tmp_path):
    """The classic leak: one toolbox handed to every agent."""
    seen = []

    def factory(_settings):
        return FakeClient([text("done")])

    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=factory)
    original = runner.build

    def spy():
        pair = original()
        seen.append(pair)
        return pair

    runner.build = spy
    runner.run("intake", "one")
    runner.run("client_comms", "two")

    toolboxes = [box for _, box in seen]
    assert len(toolboxes) == len(set(id(b) for b in toolboxes))


def test_each_run_gets_its_own_client(settings, tmp_path):
    """A reused client is reused state. The factory must be called per run."""
    built = []

    def factory(_settings):
        client = FakeClient([text("done")])
        built.append(client)
        return client

    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=factory)
    runner.run("intake", "first")
    runner.run("intake", "second")

    assert len(built) == 2
    assert built[0] is not built[1]


# --- no run inherits the last -----------------------------------------------


def test_a_run_starts_from_the_task_and_nothing_else(settings, tmp_path):
    clients = []

    def factory(_settings):
        client = FakeClient([text("done")])
        clients.append(client)
        return client

    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=factory)
    runner.run("client_comms", "FIRST TASK about the north corridor")
    runner.run("client_comms", "SECOND TASK about Room 204")

    second_messages = clients[1].requests[0]["messages"]
    assert len(second_messages) == 1
    assert second_messages[0]["content"] == "SECOND TASK about Room 204"
    # Nothing from the first run reached the second.
    transcript = str(clients[1].requests)
    assert "FIRST TASK" not in transcript
    assert "north corridor" not in transcript


def test_the_same_task_twice_does_the_work_twice(settings, tmp_path):
    """No memo, no short-circuit: the second run is as ignorant as the first."""
    def factory(_settings):
        return FakeClient([calls_tool("list_projects", {}), text("done")])

    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=factory)
    first = runner.run("client_comms", "same task")
    second = runner.run("client_comms", "same task")

    assert first.id != second.id
    assert first.tools_executed == second.tools_executed == ["list_projects"]
    assert first.iterations == second.iterations


# --- the record persists on purpose -------------------------------------------


def test_the_stored_record_survives_across_runs(settings, tmp_path):
    """Run state is discarded; what the company said and did is not."""
    from lumia.comms.seed import seed_demo_projects

    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: FakeClient([text("ok")]))
    workspace, _ = runner.build()
    project_id = seed_demo_projects(workspace)["project_id"]

    runner.run("intake", "process today's reports")

    # A completely fresh stack still sees the project.
    later, _ = runner.build()
    assert later.comms.get_project(project_id) is not None


def test_every_run_is_recorded(runner):
    record = runner.run("client_comms", "send the daily log")

    assert record.status == "finished"
    assert record.isolated is True
    assert record.tools_executed == ["list_projects"]
    assert record.finished_at
    assert record.duration_seconds >= 0

    history = runner.history()
    assert len(history) == 1
    assert history[0]["id"] == record.id
    assert history[0]["task"] == "send the daily log"


def test_history_is_newest_first_and_filterable(settings, tmp_path):
    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: FakeClient([text("ok")]))
    runner.run("intake", "one")
    runner.run("client_comms", "two")
    runner.run("intake", "three")

    assert [r["task"] for r in runner.history()] == ["three", "two", "one"]
    assert [r["task"] for r in runner.history(role="intake")] == ["three", "one"]


def test_a_failed_run_is_still_recorded(settings, tmp_path):
    class Exploding:
        def create(self, **_kwargs):
            raise RuntimeError("upstream is down")

    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: Exploding())
    record = runner.run("intake", "process reports")

    assert record.status == "failed"
    assert "upstream is down" in record.error
    assert runner.history()[0]["status"] == "failed"


def test_an_unknown_role_is_rejected_before_anything_is_built(runner):
    with pytest.raises(ValueError):
        runner.run("marketing_wizard", "do something")
    assert runner.history() == []


def test_the_facade_routes_through_isolated_runs(settings, tmp_path):
    from lumia.facade import Lumia

    lumia = Lumia(settings=settings, data_dir=tmp_path)
    lumia.runner.client_factory = lambda s: FakeClient([text("ok")])
    # Each access builds a new runner, so nothing is cached between calls.
    assert lumia.runner is not lumia.runner
