"""No communication takes longer than its budget."""

from __future__ import annotations

import time

import pytest
from conftest import FakeClient, calls_tool, text

from lumia.agent import Agent
from lumia.deadline import (
    COMMS_BUDGET_SECONDS,
    GROWTH_BUDGET_SECONDS,
    Deadline,
    DeadlineExceeded,
    budget_for,
)
from lumia.integrations.base import Integration, IntegrationError
from lumia.runner import Runner
from lumia.tools import Toolbox


# --- the budget itself ------------------------------------------------------


def test_communication_is_capped_at_two_minutes():
    assert COMMS_BUDGET_SECONDS == 120.0


def test_comms_roles_get_the_communication_budget():
    from lumia.agents import COMMS_ROLES

    for role in ("client_comms", "crew_comms", "vendor_comms", "intake", "escalation"):
        assert budget_for(role, COMMS_ROLES) == COMMS_BUDGET_SECONDS
    # Researching an account properly beats researching it quickly.
    assert budget_for("research", COMMS_ROLES) == GROWTH_BUDGET_SECONDS


def test_a_deadline_counts_down():
    deadline = Deadline(budget_seconds=120.0)
    assert deadline.remaining <= 120.0
    assert deadline.expired is False
    assert deadline.usable is True

    spent = Deadline(budget_seconds=0.0)
    assert spent.expired is True
    assert spent.usable is False
    with pytest.raises(DeadlineExceeded) as excinfo:
        spent.check("sending the log")
    assert "sending the log" in str(excinfo.value)


def test_a_deadline_uses_the_monotonic_clock():
    """A clock change must not hand a run an extra hour or cut it short."""
    before = Deadline(budget_seconds=10.0).started
    time.sleep(0.01)
    assert Deadline(budget_seconds=10.0).started > before


def test_a_call_gets_the_shorter_of_what_it_wants_and_what_is_left():
    deadline = Deadline(budget_seconds=5.0)
    assert deadline.timeout_for(20.0) <= 5.0
    assert deadline.timeout_for(1.0) == pytest.approx(1.0, abs=0.1)
    # Never zero or negative, which some clients treat as "no timeout".
    assert Deadline(budget_seconds=0.0).timeout_for(20.0) > 0


# --- the loop stops rather than overrunning ---------------------------------


def test_the_loop_refuses_to_start_a_turn_on_a_spent_budget(ws):
    agent = Agent(role="client_comms", workspace=ws, toolbox=Toolbox(ws),
                  client=FakeClient([text("should never be reached")]))
    run = agent.run("send the daily log", deadline=Deadline(budget_seconds=0.0))

    assert run.timed_out is True
    assert run.stopped_because == "deadline_exceeded"
    assert "120" not in run.reply           # it reports the real budget
    assert "Nothing was sent" in run.reply


def test_a_timed_out_run_says_what_did_and_did_not_happen(ws):
    """Silence about a partial run is the failure mode to avoid."""
    class SlowClient:
        def __init__(self, deadline):
            self.deadline = deadline
            self.turns = 0

        def create(self, **_kwargs):
            self.turns += 1
            if self.turns == 1:
                return calls_tool("list_projects", {})
            # The first tool call has landed; now the budget runs out.
            self.deadline.budget_seconds = 0.0
            return calls_tool("list_open_items", {}, use_id="toolu_2")

    deadline = Deadline(budget_seconds=60.0, label="communication")
    agent = Agent(role="client_comms", workspace=ws, toolbox=Toolbox(ws), client=SlowClient(deadline))
    run = agent.run("send the daily log", deadline=deadline)

    assert run.timed_out is True
    assert "Completed: list_projects" in run.reply
    assert "did not happen" in run.reply


def test_a_run_inside_its_budget_is_untouched(ws):
    agent = Agent(role="client_comms", workspace=ws, toolbox=Toolbox(ws),
                  client=FakeClient([calls_tool("list_projects", {}), text("done")]))
    run = agent.run("list the projects", deadline=Deadline(budget_seconds=120.0))

    assert run.timed_out is False
    assert run.stopped_because == "end_turn"
    assert run.tool_calls[0].executed is True


def test_the_runner_gives_comms_runs_the_120_second_budget(settings, tmp_path):
    runner = Runner(settings=settings, data_dir=tmp_path,
                    client_factory=lambda s: FakeClient([text("ok")]))
    record = runner.run("client_comms", "send the daily log")

    assert record.budget_seconds == 120.0
    assert record.timed_out is False


def test_a_timed_out_run_is_recorded_as_such(settings, tmp_path):
    runner = Runner(settings=settings, data_dir=tmp_path,
                    client_factory=lambda s: FakeClient([text("ok")]))
    record = runner.run("client_comms", "send the log", budget_seconds=0.0)

    assert record.timed_out is True
    assert record.stopped_because == "deadline_exceeded"
    # And it still carries its number, so it can be found afterwards.
    assert record.reference == "RUN-000001"
    assert runner.get("RUN-000001")["timed_out"] is True


# --- integrations respect what is left ---------------------------------------


class _Service(Integration):
    pass


def test_an_http_call_is_given_only_what_the_run_has_left():
    service = _Service(credentials=type("C", (), {"name": "x", "api_key": "k", "base_url": "https://x", "configured": True})())
    assert service._timeout(20.0) == 20.0        # no budget: the default

    service.budget = Deadline(budget_seconds=4.0)
    assert service._timeout(20.0) <= 4.0


def test_a_spent_budget_stops_a_call_before_it_starts():
    """Refusing is faster than failing, and says something more useful."""
    credentials = type("C", (), {"name": "email", "api_key": "k", "base_url": "https://x", "configured": True})()
    service = _Service(credentials=credentials)
    service.budget = Deadline(budget_seconds=0.0)

    with pytest.raises(IntegrationError) as excinfo:
        service.request("POST", "/mail/send", json={}, mock={})
    assert "time budget is spent" in str(excinfo.value)


def test_retries_do_not_outlive_the_budget(monkeypatch):
    """Three retries at twenty seconds each cannot eat half a run."""
    import httpx

    from lumia.integrations import base

    attempts = {"n": 0}

    def always_503(*_args, **_kwargs):
        attempts["n"] += 1
        return httpx.Response(503, text="busy", request=httpx.Request("POST", "https://x/y"))

    monkeypatch.setattr(base.httpx, "request", always_503)
    monkeypatch.setattr(base.time, "sleep", lambda _s: None)

    credentials = type("C", (), {"name": "email", "api_key": "k", "base_url": "https://x", "configured": True})()
    service = _Service(credentials=credentials)
    service.budget = Deadline(budget_seconds=2.0)   # room for one attempt, not a retry

    with pytest.raises(IntegrationError):
        service.request("POST", "/send", json={}, mock={})
    assert attempts["n"] == 1, "it retried into a budget that could not afford it"


def test_transcription_never_outlasts_the_run(ws):
    """Whisper asks for 90s; a communication run does not have 90s to give."""
    from lumia.integrations.openai import TRANSCRIBE_TIMEOUT_SECONDS

    assert TRANSCRIBE_TIMEOUT_SECONDS < COMMS_BUDGET_SECONDS

    ws.openai.budget = Deadline(budget_seconds=10.0)
    assert ws.openai._timeout(TRANSCRIBE_TIMEOUT_SECONDS) <= 10.0

    # A configured service refuses rather than dialling out on a spent
    # budget. (An unconfigured one returns its flagged mock and costs
    # nothing, so there is no call to cut short.)
    from lumia.config import ServiceCredentials
    from lumia.integrations.openai import OpenAIService

    live = OpenAIService(ServiceCredentials(name="openai", api_key="sk-test", base_url="https://x"))
    live.budget = Deadline(budget_seconds=0.0)
    assert "error" in live.transcribe(b"audio")
    assert "budget is spent" in live.transcribe(b"audio")["error"]


def test_the_budget_reaches_every_integration(ws):
    deadline = Deadline(budget_seconds=30.0)
    ws.set_deadline(deadline)

    for service in (ws.email, ws.sms, ws.crm, ws.calendar, ws.search, ws.construction, ws.openai, ws.weather):
        assert service.budget is deadline, f"{service.name} was left without a budget"
