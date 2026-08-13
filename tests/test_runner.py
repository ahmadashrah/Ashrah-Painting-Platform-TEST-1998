"""Isolation: agents share nothing, and no run inherits the last one."""

from __future__ import annotations

import pytest
from conftest import FakeClient, PhaseClient, calls_tool, text

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
    # The phase wraps the task, but the task is what the phase is given.
    assert "SECOND TASK about Room 204" in second_messages[0]["content"]
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


# --- numbering: every run, every agent, issued at creation ------------------


def test_a_run_is_numbered_before_it_does_anything(runner):
    record = runner.run("intake", "process reports")
    assert record.number == 1
    assert record.reference == "RUN-000001"


def test_numbers_are_unique_and_increase_across_agents(settings, tmp_path):
    """One sequence for the whole platform, not one per agent."""
    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: FakeClient([text("ok")]))
    references = [runner.run(role, "go").reference
                  for role in ("intake", "client_comms", "director", "vendor_comms", "intake")]

    assert references == ["RUN-000001", "RUN-000002", "RUN-000003", "RUN-000004", "RUN-000005"]
    assert len(set(references)) == len(references)


def test_a_failed_run_still_keeps_its_number(settings, tmp_path):
    """A run that dies on its first turn must still be findable."""
    class Exploding:
        def create(self, **_kwargs):
            raise RuntimeError("upstream is down")

    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: Exploding())
    record = runner.run("intake", "process reports")

    assert record.reference == "RUN-000001"
    assert record.status == "failed"
    assert runner.get("RUN-000001")["status"] == "failed"


def test_concurrent_runs_never_share_a_number(settings, tmp_path):
    import threading

    from lumia.runner import RunCounter

    counter = RunCounter(tmp_path / "run-counter")
    taken: list[int] = []
    lock = threading.Lock()

    def grab():
        mine = [counter.next() for _ in range(25)]
        with lock:
            taken.extend(mine)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(taken) == 200
    assert len(set(taken)) == 200, "two runs were handed the same number"
    assert sorted(taken) == list(range(1, 201))


def test_a_lost_counter_never_reissues_a_number(settings, tmp_path):
    """Skipping a range is survivable; two runs sharing a reference is not."""
    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: FakeClient([text("ok")]))
    runner.run("intake", "one")
    runner.run("intake", "two")

    (tmp_path / "run-counter").unlink()
    runner._counter = None

    assert runner.run("intake", "three").number == 3


def test_records_carry_the_run_that_wrote_them(settings, tmp_path):
    """The number is only useful if it reaches what the run produced."""
    from lumia.comms.seed import seed_demo_projects

    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: FakeClient([text("ok")]))
    workspace, _ = runner.build()
    project = seed_demo_projects(workspace)["project_id"]

    def factory(_settings):
        # Escalating belongs to the resolve phase, so that is where it is scripted.
        return PhaseClient({"resolve": [
            calls_tool("raise_escalation", {
                "project_id": project, "category": "site_access",
                "summary": "Access blocked all morning.", "evidence": "Field report.",
                "impact": "Half a day lost.", "recommended_action": "Confirm the access window.",
            }),
            text("Escalated."),
        ]})

    runner.client_factory = factory
    record = runner.run("intake", "report the access problem")

    later, _ = runner.build()
    escalation = later.store.list("escalations")[0]
    assert escalation["_run"] == record.reference


def test_a_run_can_be_traced_to_everything_it_touched(settings, tmp_path):
    from lumia.comms.seed import seed_demo_projects

    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: FakeClient([text("ok")]))
    workspace, _ = runner.build()
    project = seed_demo_projects(workspace)["project_id"]

    runner.client_factory = lambda s: PhaseClient({"track": [
        calls_tool("raise_open_item", {
            "project_id": project, "question": "Confirm the access window.",
            "asked_of": "Marcus Reyes (DEMO)",
        }),
        text("Asked."),
    ]})
    record = runner.run("crew_comms", "chase the access question")

    trace = runner.trace(record.reference)
    assert trace["run"]["reference"] == record.reference
    assert "open_items" in trace["touched"]

    assert "error" in runner.trace("RUN-999999")


def test_a_run_is_findable_by_number_or_reference(runner):
    record = runner.run("intake", "process reports")
    for lookup in (record.reference, "1", "run-000001", "RUN-1"):
        found = runner.get(lookup)
        assert found is not None, f"could not find the run by {lookup!r}"
        assert found["reference"] == record.reference


def test_history_orders_by_run_number(settings, tmp_path):
    runner = Runner(settings=settings, data_dir=tmp_path, client_factory=lambda s: FakeClient([text("ok")]))
    for task in ("one", "two", "three"):
        runner.run("intake", task)
    assert [r["reference"] for r in runner.history()] == ["RUN-000003", "RUN-000002", "RUN-000001"]


def test_records_written_outside_a_run_are_not_stamped(ws):
    """Seeding and manual tool calls are not runs and must not claim to be."""
    from lumia.comms.seed import seed_demo_projects

    project_id = seed_demo_projects(ws)["project_id"]
    assert "_run" not in ws.comms.get_project(project_id)


def test_the_standing_cycles_are_numbered_too(settings, tmp_path):
    """A cycle that skipped numbering would be the run nobody could trace."""
    from lumia.comms.desk import CommunicationDesk
    from lumia.tools import Toolbox
    from lumia.workspace import Workspace

    workspace = Workspace.build(settings=settings, data_dir=tmp_path)
    desk = CommunicationDesk(workspace=workspace, toolbox=Toolbox(workspace),
                             client=FakeClient([text("nothing to do")]))

    assert desk.followups().reference == "RUN-000001"
    assert desk.intake().reference == "RUN-000002"


def test_growth_cycles_share_the_same_sequence(settings, tmp_path):
    from lumia.comms.desk import CommunicationDesk
    from lumia.orchestrator import Orchestrator
    from lumia.tools import Toolbox
    from lumia.workspace import Workspace

    workspace = Workspace.build(settings=settings, data_dir=tmp_path)
    client = FakeClient([text("ok")] * 10)
    growth = Orchestrator(workspace=workspace, toolbox=Toolbox(workspace), client=client)
    comms = CommunicationDesk(workspace=workspace, toolbox=Toolbox(workspace), client=client)

    assert growth.handle("check the pipeline").reference == "RUN-000001"
    assert comms.followups().reference == "RUN-000002"
    assert growth.weekly_review().reference == "RUN-000003"
