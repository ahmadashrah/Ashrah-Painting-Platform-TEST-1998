"""The daily log, channel selection and the performance metrics."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from lumia.comms import dailylog
from lumia.comms.channels import choose_channel
from lumia.comms.reporting import communication_review
from lumia.comms.seed import seed_demo_projects
from lumia.domain.projects import CommChannel, CommKind, DailyLog
from lumia.tools import Toolbox


@pytest.fixture
def project(ws):
    return seed_demo_projects(ws)["project_id"]


@pytest.fixture
def box(ws):
    return Toolbox(ws)


# --- channel selection ---------------------------------------------------


def test_attachments_force_email_over_a_text_preference():
    choice = choose_channel(
        kind=CommKind.ARRIVAL_NOTICE, recipient_preference=CommChannel.SMS, has_attachments=True
    )
    assert choice.channel is CommChannel.EMAIL
    assert choice.overrode_preference is True


def test_a_formal_record_goes_by_email():
    assert choose_channel(kind=CommKind.DAILY_LOG).channel is CommChannel.EMAIL
    assert choose_channel(kind=CommKind.CLIENT_UPDATE).channel is CommChannel.EMAIL


def test_a_long_message_goes_by_email():
    choice = choose_channel(kind=CommKind.ARRIVAL_NOTICE, body_length=900)
    assert choice.channel is CommChannel.EMAIL
    assert "characters" in choice.reason


def test_short_and_urgent_goes_by_text():
    choice = choose_channel(kind=CommKind.CONFIRMATION_REQUEST, urgency="urgent", body_length=100)
    assert choice.channel is CommChannel.SMS


def test_stated_preference_wins_an_ordinary_message():
    choice = choose_channel(kind=CommKind.OTHER, recipient_preference=CommChannel.SMS, body_length=80)
    assert choice.channel is CommChannel.SMS
    assert "preference" in choice.reason


def test_every_channel_choice_is_recorded_in_the_project():
    assert choose_channel(kind=CommKind.ARRIVAL_NOTICE).to_dict()["recorded_in_project"] is True


# --- daily log -----------------------------------------------------------


def test_a_log_with_no_work_is_refused():
    problems = dailylog.validate(DailyLog(project_id="proj_1"))
    assert any("no verified completed or in-progress work" in p for p in problems)


def test_an_unverified_completion_claim_is_refused():
    log = DailyLog(project_id="proj_1", completed=["Level 2 is 100% complete."])
    assert any("Unverified completion claim" in p for p in dailylog.validate(log))

    # With a verification behind it, the same sentence is allowed.
    assert dailylog.validate(log, completion_verified=True) == []


def test_next_day_items_without_hedging_warn():
    log = DailyLog(project_id="proj_1", completed=["Primer applied."], next_day=["We finish the corridor."])
    assert any("reads as a commitment" in w for w in dailylog.warnings(log))

    hedged = DailyLog(
        project_id="proj_1",
        completed=["Primer applied."],
        next_day=["First finish coat is planned, subject to site readiness."],
    )
    assert not any("reads as a commitment" in w for w in dailylog.warnings(hedged))


def test_internal_notes_are_structurally_unable_to_reach_the_client(ws, box, project):
    result = box.call(
        "compose_daily_log",
        {
            "project_id": project,
            "completed": ["Primer applied to corridor walls."],
            "unverified_notes": ["Crew lead first reported 20 hours; corrected against the clock."],
        },
    )
    assert "20 hours" not in result["body"]
    assert result["unverified_notes"]          # recorded, just not rendered


def test_the_rendered_log_follows_the_standard_layout(ws, box, project):
    result = box.call(
        "compose_daily_log",
        {
            "project_id": project,
            "completed": ["Surface preparation completed in the north corridor."],
            "in_progress": ["Room 204 prepared; coating paused."],
            "issues": ["Please confirm who will address the moisture source in Room 204."],
            "next_day": ["First finish coat is planned, subject to site readiness."],
            "crew_count": 2,
        },
    )
    body = result["body"]
    assert result["subject"].startswith("Daily Project Update – ")
    for heading in (
        "Work Completed Today",
        "Work in Progress",
        "Issues Requiring Attention",
        "Plan for the Next Working Day",
        "Safety",
    ):
        assert heading in body
    assert "Ashrah Painting Crew: 2 workers" in body
    assert body.rstrip().endswith("Kind regards,\nAshrah Painting Ltd.")


def test_empty_optional_sections_are_omitted_but_issues_and_safety_are_not(ws, box, project):
    result = box.call(
        "compose_daily_log", {"project_id": project, "completed": ["Primer applied to corridor walls."]}
    )
    assert "Materials and Equipment" not in result["body"]
    assert "Issues Requiring Attention\nNone at this time." in result["body"]
    assert "Safety\nNo safety concerns were reported today." in result["body"]


def test_headcount_is_withheld_when_the_client_treats_it_as_private(ws, box, project):
    ws.comms.upsert_project({"id": project, "share_crew_count": False, "share_work_period": False})
    result = box.call(
        "compose_daily_log",
        {"project_id": project, "completed": ["Primer applied."], "crew_count": 2, "work_period": "18:00 – 02:00"},
    )
    assert "Ashrah Painting Crew" not in result["body"]
    assert "Work Period" not in result["body"]


def test_uncleared_photos_cannot_be_attached_to_a_log(ws, box, project):
    uncaptioned = ws.comms.media_for(project)[1]
    result = box.call(
        "compose_daily_log",
        {"project_id": project, "completed": ["Primer applied."], "photo_ids": [uncaptioned["id"]]},
    )
    assert "error" in result
    assert "not cleared" in result["error"]


def test_gathering_reports_the_gaps(ws, box, project):
    facts = box.call("build_daily_log", {"project_id": project})
    assert facts["ready"] is False
    assert any("translation" in gap or "still need" in gap for gap in facts["gaps"])
    assert any("flagged and withheld" in gap for gap in facts["gaps"])
    assert facts["verified_crew_count"] == 2
    assert facts["verified_hours"] == 15.5


# --- performance metrics -------------------------------------------------


def test_rates_are_null_rather_than_zero_when_nothing_happened(ws):
    review = communication_review(ws, days=7)
    assert review["delivery"]["delivery_success_rate"] is None
    assert review["accuracy"]["translation_coverage"] is None
    assert review["responsiveness"]["median_response_days"] is None


def test_metrics_count_what_actually_happened(ws, box, project):
    media = ws.comms.media_for(project)[0]
    box.call("caption_media", {"media_id": media["id"], "caption": "Primer applied.", "client_safe": True})
    log = box.call("compose_daily_log", {"project_id": project, "completed": ["Primer applied."]})
    draft = box.call(
        "draft_communication",
        {
            "project_id": project,
            "recipient": "Dana Whitfield (DEMO)",
            "kind": "daily_log",
            "subject": log["subject"],
            "body": log["body"],
            "source_ids": [log["id"]],
            "requires_response": True,
        },
    )
    box.call("send_communication", {"draft_id": draft["id"]})
    box.call(
        "log_communication_response",
        {"communication_id": draft["id"], "summary": "Acknowledged, will action the moisture.", "sentiment": "positive"},
    )

    review = communication_review(ws, days=7)
    assert review["volume"]["daily_logs_sent"] == 1
    assert review["volume"]["messages_sent"] == 1
    assert review["delivery"]["delivery_success_rate"] == 1.0
    assert review["delivery"]["verified_recipient_rate"] == 1.0
    assert review["responsiveness"]["replies_received"] == 1
    assert review["responsiveness"]["median_response_days"] == 0.0
    assert review["accuracy"]["translation_coverage"] == 0.0     # the Arabic report is untranslated


def test_gated_sends_are_counted_as_prevented(ws, box, project):
    from lumia.autonomy import ApprovalRequest

    ws.approvals.submit(
        ApprovalRequest(tool="send_communication", arguments={}, reason="pricing", project_id=project)
    )
    review = communication_review(ws, days=7)
    assert review["safety_net"]["unsafe_automatic_sends_prevented"] == 1
    assert review["safety_net"]["still_awaiting_approval"] == 1


def test_open_items_and_overdue_are_tracked(ws, box, project):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    box.call(
        "raise_open_item",
        {
            "project_id": project,
            "question": "Confirm who will address the moisture source in Room 204.",
            "asked_of": "Dana Whitfield (DEMO)",
            "due_date": yesterday,
        },
    )
    listing = box.call("list_open_items", {"project_id": project})
    assert listing["count"] == 1
    assert len(listing["overdue"]) == 1

    review = communication_review(ws, days=7)
    assert review["responsiveness"]["overdue_items"] == 1


def test_answering_an_item_closes_it(ws, box, project):
    item = box.call(
        "raise_open_item",
        {"project_id": project, "question": "Confirm the access window.", "asked_of": "Marcus Reyes (DEMO)"},
    )
    box.call("close_open_item", {"item_id": item["id"], "answer": "Access confirmed from 18:00."})
    assert box.call("list_open_items", {"project_id": project})["count"] == 0


def test_a_failed_delivery_is_not_counted_as_sent(ws, box, project):
    draft = box.call(
        "draft_communication",
        {
            "project_id": project,
            "recipient": "Dana Whitfield (DEMO)",
            "kind": "daily_log",
            "subject": "Update",
            "body": "Primer applied to corridor walls.",
        },
    )
    ws.comms.mark_sent(draft["id"], {"status": "failed", "error": "mailbox unavailable"})

    assert len(ws.comms.failed_deliveries(project)) == 1
    review = communication_review(ws, days=7)
    assert review["delivery"]["failed_deliveries"] == 1
    assert review["delivery"]["delivery_success_rate"] == 0.0
    assert box.call("unanswered_communications", {})["failed_deliveries"]


def test_escalation_requires_the_full_package(box, project):
    result = box.call("raise_escalation", {"project_id": project, "category": "schedule_delay", "summary": "late"})
    assert "error" in result
    assert "evidence" in result["error"]


def test_a_complete_escalation_is_recorded(ws, box, project):
    result = box.call(
        "raise_escalation",
        {
            "project_id": project,
            "category": "potential_change_order",
            "summary": "Crew reported ceiling painting, which the contract excludes.",
            "evidence": "Field submission reported ceiling work; project exclusions list ceiling painting.",
            "impact": "Unbilled labour and a possible change order.",
            "recommended_action": "Confirm with the superintendent whether the work was directed.",
        },
    )
    assert result["status"] == "open"
    assert "requires approval" in result["notice"]
    assert len(ws.comms.escalations(project, status="open")) == 1
