"""The send gate: what may go out on its own, and what may not.

This is the safety-critical half of the Communication Agent. The rule
being tested throughout is that the level is decided by the *stored*
message — its kind, its recipient and its actual words — not by what the
call claims or what the agent intended.
"""

from __future__ import annotations

import pytest
from conftest import FakeClient, calls_tool, text

from lumia.agent import Agent
from lumia.autonomy import AutonomyLevel, classify
from lumia.comms.seed import seed_demo_projects
from lumia.domain.projects import DeliveryStatus
from lumia.tools import Toolbox


@pytest.fixture
def project(ws):
    return seed_demo_projects(ws)["project_id"]


@pytest.fixture
def box(ws):
    return Toolbox(ws)


def _draft(box, project, **overrides):
    payload = {
        "project_id": project,
        "recipient": "Dana Whitfield (DEMO)",
        "kind": "daily_log",
        "subject": "Daily Project Update",
        "body": "Surface preparation completed in the north corridor. Primer applied to corridor walls.",
    }
    payload.update(overrides)
    return box.call("draft_communication", payload)


def _level(ws, draft_id):
    return classify("send_communication", {"draft_id": draft_id}, None, ws.comms.get_communication(draft_id))


# --- what may send on its own -------------------------------------------


def test_routine_daily_log_is_level_two(ws, box, project):
    draft = _draft(box, project)
    assert draft["can_auto_send"] is True

    level, reason = _level(ws, draft["id"])
    assert level is AutonomyLevel.CONTROLLED
    assert "daily_log" in reason


def test_arrival_notice_to_the_super_is_level_two(ws, box, project):
    draft = _draft(
        box,
        project,
        recipient="Marcus Reyes (DEMO)",
        kind="arrival_notice",
        subject="Arrival",
        body="Crew arriving on site at 18:00 tonight for the north corridor.",
    )
    level, _ = _level(ws, draft["id"])
    assert level is AutonomyLevel.CONTROLLED


# --- what may not -------------------------------------------------------


def test_a_price_inside_a_routine_log_escalates_it(ws, box, project):
    draft = _draft(box, project, body="Primer applied to corridor walls. The extra ceiling work will be $4,200.")
    assert draft["can_auto_send"] is False

    level, reason = _level(ws, draft["id"])
    assert level is AutonomyLevel.APPROVAL_REQUIRED
    assert "pricing" in reason


def test_a_completion_guarantee_escalates_an_arrival_notice(ws, box, project):
    draft = _draft(
        box,
        project,
        recipient="Marcus Reyes (DEMO)",
        kind="arrival_notice",
        body="Crew arriving at 18:00. Level 2 is 100% complete.",
    )
    level, reason = _level(ws, draft["id"])
    assert level is AutonomyLevel.APPROVAL_REQUIRED
    assert "completion_guarantee" in reason


def test_a_non_routine_kind_needs_approval_whatever_it_says(ws, box, project):
    """Kind is checked before content: a commitment is Level 3 even if it reads clean."""
    draft = _draft(box, project, kind="commitment", body="Confirming the arrangement we discussed.")
    level, reason = _level(ws, draft["id"])
    assert level is AutonomyLevel.APPROVAL_REQUIRED
    assert "routine" in reason


def test_an_unreadable_draft_is_never_sent(ws):
    level, reason = classify("send_communication", {"draft_id": "comm_missing"}, None, None)
    assert level is AutonomyLevel.APPROVAL_REQUIRED
    assert "could not be read" in reason


def test_an_unverified_recipient_is_never_sent(ws, box, project):
    draft = _draft(box, project)
    ws.store.patch("communications", draft["id"], {"recipient_verified": False})
    level, reason = _level(ws, draft["id"])
    assert level is AutonomyLevel.APPROVAL_REQUIRED
    assert "recorded contacts" in reason


def test_the_gate_screens_the_stored_body_not_the_arguments(ws, box, project):
    """Editing the record after drafting still changes the verdict."""
    draft = _draft(box, project)
    assert _level(ws, draft["id"])[0] is AutonomyLevel.CONTROLLED

    ws.store.patch("communications", draft["id"], {"body": "We guarantee completion by Friday."})
    assert _level(ws, draft["id"])[0] is AutonomyLevel.APPROVAL_REQUIRED


def test_material_orders_always_need_approval():
    level, _ = classify("place_material_order", {"project_id": "proj_1"})
    assert level is AutonomyLevel.APPROVAL_REQUIRED


def test_escalation_to_management_is_not_gated():
    """Telling a manager about an injury must never wait in a queue."""
    level, _ = classify("raise_escalation", {"project_id": "proj_1"})
    assert level is AutonomyLevel.CONTROLLED


def test_every_registered_tool_is_classified(ws):
    """A tool missing from the table is gated on every call — catch that here."""
    from lumia.autonomy import TOOL_LEVELS

    unclassified = [name for name in Toolbox(ws).names() if name not in TOOL_LEVELS]
    assert unclassified == []


# --- recipients and attachments -------------------------------------------


def test_an_unknown_recipient_is_refused(box, project):
    result = box.call(
        "draft_communication",
        {"project_id": project, "recipient": "someone@guessed.com", "subject": "x", "body": "y"},
    )
    assert "error" in result
    assert "never guess an address" in result["error"]
    assert result["known_contacts"]


def test_a_flagged_photo_cannot_be_attached_for_a_client(ws, box, project):
    security = [m for m in ws.comms.media_for(project) if "security" in m["uri"]][0]
    box.call(
        "caption_media",
        {"media_id": security["id"], "caption": "Level 1 keypad.", "flags": ["security"], "client_safe": True},
    )
    result = _draft(box, project, attachments=[security["id"]])
    assert "error" in result
    assert "not cleared" in result["error"]


def test_channel_falls_back_to_email_when_attachments_are_present(ws, box, project):
    media = ws.comms.media_for(project)[0]
    box.call("caption_media", {"media_id": media["id"], "caption": "Primer applied.", "client_safe": True})

    draft = _draft(box, project, recipient="Marcus Reyes (DEMO)", attachments=[media["id"]])
    assert draft["channel"] == "email"          # despite his SMS preference
    assert draft["channel_choice"]["overrode_preference"] is True


def test_drafting_never_sends(ws, box, project):
    draft = _draft(box, project)
    assert ws.comms.get_communication(draft["id"])["status"] == DeliveryStatus.DRAFT.value


def test_sending_records_delivery_and_marks_the_log_sent(ws, box, project):
    log = box.call(
        "compose_daily_log",
        {"project_id": project, "completed": ["Primer applied to corridor walls."], "crew_count": 2},
    )
    draft = _draft(box, project, source_ids=[log["id"]])
    result = box.call("send_communication", {"draft_id": draft["id"]})

    assert result["status"] == DeliveryStatus.SENT.value
    stored = ws.comms.get_communication(draft["id"])
    assert stored["sent_at"]
    assert stored["approval_source"] == "level_2_autonomous"
    assert ws.comms.get_daily_log(log["id"])["status"] == "sent"


def test_a_message_cannot_be_sent_twice(box, project):
    draft = _draft(box, project)
    box.call("send_communication", {"draft_id": draft["id"]})
    assert "error" in box.call("send_communication", {"draft_id": draft["id"]})


def test_approved_sends_record_who_cleared_them(ws, box, project):
    draft = _draft(box, project, kind="commitment")
    box.call("send_communication", {"draft_id": draft["id"], "approval_id": "appr_123"})

    stored = ws.comms.get_communication(draft["id"])
    assert stored["approval_id"] == "appr_123"
    assert stored["approval_source"] == "human_approval"


# --- through the real agent loop --------------------------------------------


def test_the_loop_queues_a_risky_send_and_holds_the_draft(ws, project):
    box = Toolbox(ws)
    draft = _draft(box, project, body="Primer applied. The additional ceiling work will be $4,200.")

    agent = Agent(
        role="client_comms",
        workspace=ws,
        toolbox=box,
        client=FakeClient(
            [
                calls_tool("send_communication", {"draft_id": draft["id"]}),
                text("That is queued for approval — I have not sent it."),
            ]
        ),
    )
    run = agent.run("send today's log to Dana")

    assert len(run.approvals_raised) == 1
    assert run.tool_calls[0].executed is False
    assert run.tool_calls[0].level == 3

    # Nothing went out, and the draft is visibly held rather than abandoned.
    stored = ws.comms.get_communication(draft["id"])
    assert stored["status"] == DeliveryStatus.AWAITING_APPROVAL.value
    assert stored["approval_id"] == run.approvals_raised[0]
    assert not stored["sent_at"]

    request = ws.approvals.pending()[0]
    assert request["project_id"] == project


def test_the_loop_sends_a_clean_routine_message(ws, project):
    box = Toolbox(ws)
    draft = _draft(box, project)

    agent = Agent(
        role="client_comms",
        workspace=ws,
        toolbox=box,
        client=FakeClient([calls_tool("send_communication", {"draft_id": draft["id"]}), text("Sent.")]),
    )
    run = agent.run("send today's log to Dana")

    assert run.approvals_raised == []
    assert run.tool_calls[0].executed is True
    assert ws.comms.get_communication(draft["id"])["status"] == DeliveryStatus.SENT.value
