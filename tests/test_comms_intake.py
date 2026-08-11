"""Field intake: traceability, verification and photo handling."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from lumia.comms.seed import seed_demo_projects
from lumia.domain.projects import Verification
from lumia.tools import Toolbox


@pytest.fixture
def project(ws):
    return seed_demo_projects(ws)["project_id"]


@pytest.fixture
def box(ws):
    return Toolbox(ws)


def test_original_submission_is_never_overwritten(ws, box, project):
    submission = ws.comms.submissions_for(project)[0]
    original = submission["transcript"]

    box.call(
        "process_field_submission",
        {
            "submission_id": submission["id"],
            "translation_en": "We finished preparation in the north corridor.",
            "normalized_summary": "Surface preparation completed in the north corridor.",
        },
    )

    stored = ws.comms.get_submission(submission["id"])
    assert stored["transcript"] == original
    assert stored["original_language"] == "ar"
    assert stored["translation_en"].startswith("We finished")
    # Every change is on the record, in order.
    assert [r["action"] for r in stored["revisions"]] == [
        "submitted",
        "translated_and_normalized",
        "verified",
    ]


def test_rewriting_the_original_is_refused(ws, project):
    submission = ws.comms.submissions_for(project)[0]
    result = ws.comms.revise_submission(
        submission["id"], {"original_text": "something else"}, action="tamper"
    )
    assert "error" in result
    assert "preserved for traceability" in result["error"]


def test_reported_hours_are_checked_against_the_clock(ws, box, project):
    """The seeded submission claims 20 hours against 15.5 clocked."""
    submission = ws.comms.submissions_for(project)[0]
    checks = box.call("verify_field_submission", {"submission_id": submission["id"]})

    assert checks["verification"] == Verification.CONFLICTED.value
    assert checks["clocked_hours"] == 15.5
    assert any("20.0 labour hours" in c for c in checks["conflicts"])


def test_work_against_a_scope_exclusion_is_flagged(ws, box, project):
    submission = ws.comms.submissions_for(project)[0]
    result = box.call(
        "process_field_submission",
        {
            "submission_id": submission["id"],
            "normalized_summary": "Ceiling painting carried out in the north corridor.",
            "activities": ["ceiling painting"],
            "hours_reported": 15.5,
        },
    )
    assert any("exclusion" in c for c in result["checks"]["conflicts"])


def test_untranslated_submission_is_reported_as_missing(ws, box, project):
    submission = ws.comms.submissions_for(project)[0]
    checks = box.call("verify_field_submission", {"submission_id": submission["id"]})
    assert any("has not been translated" in m for m in checks["missing"])


def test_future_dated_work_is_a_conflict(ws, box, project):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    created = box.call(
        "record_field_submission",
        {
            "project_id": project,
            "original_text": "Finished the west stairwell.",
            "original_language": "en",
            "work_date": tomorrow,
        },
    )
    assert any("in the future" in c for c in created["checks"]["conflicts"])


def test_duplicate_submission_from_one_employee_is_flagged(ws, box, project):
    crew = ws.comms.crew_for(project)[0]
    box.call(
        "record_field_submission",
        {
            "project_id": project,
            "original_text": "Second report of the day.",
            "original_language": "en",
            "submitted_by": crew["id"],
        },
    )
    submissions = ws.comms.submissions_for(project)
    latest = box.call("verify_field_submission", {"submission_id": submissions[-1]["id"]})
    assert any("already submitted a report" in c for c in latest["conflicts"])


def test_submission_needs_some_content(ws, box, project):
    assert "error" in box.call("record_field_submission", {"project_id": project, "original_text": "  "})


def test_submission_against_an_unknown_project_is_refused(box):
    assert "error" in box.call(
        "record_field_submission", {"project_id": "proj_nope", "original_text": "hello"}
    )


def test_caption_rejects_a_completion_claim(ws, box, project):
    media = ws.comms.media_for(project)[0]
    result = box.call("caption_media", {"media_id": media["id"], "caption": "Room 204 is fully complete."})
    assert "error" in result
    assert "cannot prove the work is finished" in result["error"]


def test_caption_is_required(ws, box, project):
    media = ws.comms.media_for(project)[0]
    assert "error" in box.call("caption_media", {"media_id": media["id"], "caption": "   "})


def test_a_flagged_photo_cannot_be_marked_client_safe(ws, box, project):
    security = [m for m in ws.comms.media_for(project) if "security" in m["uri"]][0]
    result = box.call(
        "caption_media",
        {
            "media_id": security["id"],
            "caption": "Level 1 entry keypad.",
            "flags": ["security"],
            "client_safe": True,
        },
    )
    assert result["client_safe"] is False
    assert result["blocked"] is True
    assert "withheld" in result["notice"]


def test_unknown_flags_are_dropped_rather_than_stored(ws, box, project):
    media = ws.comms.media_for(project)[0]
    result = box.call(
        "caption_media",
        {"media_id": media["id"], "caption": "Primer application in progress.", "flags": ["weird", "blurry"]},
    )
    assert result["flags"] == ["blurry"]


def test_media_listing_separates_safe_from_reviewable(ws, box, project):
    media = ws.comms.media_for(project)
    box.call("caption_media", {"media_id": media[0]["id"], "caption": "Primer applied.", "client_safe": True})
    box.call(
        "caption_media",
        {"media_id": media[2]["id"], "caption": "Keypad.", "flags": ["security"], "client_safe": True},
    )

    listing = box.call("list_project_media", {"project_id": project})
    safe_ids = {m["id"] for m in listing["client_safe"]}
    assert media[0]["id"] in safe_ids
    assert media[2]["id"] not in safe_ids
    assert media[1]["id"] in listing["uncaptioned"]


def test_processing_requires_a_normalized_summary(ws, box, project):
    submission = ws.comms.submissions_for(project)[0]
    assert "error" in box.call(
        "process_field_submission", {"submission_id": submission["id"], "normalized_summary": " "}
    )


def test_seed_is_idempotent(ws):
    first = seed_demo_projects(ws)["project_id"]
    second = seed_demo_projects(ws)["project_id"]
    assert first != second
    assert len(ws.comms.list_projects()) == 1
    assert len(ws.comms.submissions_for(second)) == 1
