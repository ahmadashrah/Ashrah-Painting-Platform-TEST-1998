"""The loop must enforce the gate, not just report on it."""

from __future__ import annotations

from conftest import FakeClient, calls_tool, text

from lumia.agent import Agent
from lumia.domain.accounts import Account, AccountTier, AccountType, PipelineStage
from lumia.tools import Toolbox


def _agent(ws, responses, role="director"):
    toolbox = Toolbox(ws)
    return Agent(role=role, workspace=ws, toolbox=toolbox, client=FakeClient(responses))


def test_level_one_tool_executes(ws):
    agent = _agent(ws, [calls_tool("pipeline_report", {}), text("Pipeline is empty.")])
    run = agent.run("what does the pipeline look like?")

    assert run.stopped_because == "end_turn"
    assert len(run.tool_calls) == 1
    assert run.tool_calls[0].executed is True
    assert run.approvals_raised == []


def test_level_three_tool_is_queued_not_executed(ws):
    account = Account(name="Test GC", tier=AccountTier.A, stage=PipelineStage.RESEARCHED, source="test")
    ws.crm.upsert_account(account.to_dict())

    agent = _agent(
        ws,
        [
            calls_tool(
                "send_first_contact_email",
                {
                    "account_id": account.id,
                    "to": "someone@example.com",
                    "subject": "Hello",
                    "body": "Hi there",
                },
            ),
            text("Queued for approval; I did not send it."),
        ],
        role="outreach",
    )
    run = agent.run("email the estimator at Test GC")

    assert len(run.approvals_raised) == 1
    assert run.tool_calls[0].executed is False
    # Nothing was logged against the account, because nothing was sent.
    assert ws.crm.interactions_for(account.id) == []
    assert len(ws.approvals.pending()) == 1


def test_followup_to_tier_a_prospect_escalates(ws):
    """A Level 2 tool becomes Level 3 because of who it targets."""
    account = Account(
        name="Strategic GC",
        account_type=AccountType.GENERAL_CONTRACTOR,
        tier=AccountTier.A,
        stage=PipelineStage.PROSPECT,
        source="test",
    )
    ws.crm.upsert_account(account.to_dict())

    agent = _agent(
        ws,
        [
            calls_tool(
                "send_followup_email",
                {"account_id": account.id, "to": "x@example.com", "subject": "Hi", "body": "Hi"},
            ),
            text("Queued."),
        ],
        role="outreach",
    )
    run = agent.run("follow up with Strategic GC")

    assert run.tool_calls[0].executed is False
    assert run.tool_calls[0].level == 3


def test_refusal_is_handled_before_reading_content(ws):
    from conftest import FakeResponse

    agent = _agent(ws, [FakeResponse(content=[], stop_reason="refusal")])
    run = agent.run("do something disallowed")

    assert run.stopped_because == "refusal"
    assert "declined" in run.reply


def test_iteration_limit_stops_the_loop(ws):
    agent = _agent(ws, [calls_tool("pipeline_report", {}, use_id=f"toolu_{i}") for i in range(10)])
    run = agent.run("loop forever", max_iterations=3)

    assert run.stopped_because == "iteration_limit"
    assert run.iterations == 3


def test_assistant_content_is_echoed_back_verbatim(ws):
    """Thinking blocks must survive the round trip, so content is appended whole."""
    client = FakeClient([calls_tool("pipeline_report", {}), text("done")])
    agent = Agent(role="director", workspace=ws, toolbox=Toolbox(ws), client=client)
    agent.run("check the pipeline")

    second_request = client.requests[1]["messages"]
    assistant_turn = second_request[1]
    assert assistant_turn["role"] == "assistant"
    # The raw content list is passed through, not a re-serialized string.
    assert isinstance(assistant_turn["content"], list)
