"""Content screening decides what a human has to see. It is worth testing hard."""

from __future__ import annotations

import pytest

from lumia.comms.screening import screen, screen_record
from lumia.domain.projects import RecipientRole


@pytest.mark.parametrize(
    "text, category",
    [
        ("The extra ceiling work will be $4,200.", "pricing"),
        ("We will issue a change order for the additional scope.", "change_order"),
        ("We guarantee the finish for five years.", "contractual_commitment"),
        ("Level 2 is 100% complete.", "completion_guarantee"),
        ("We will be finished by Friday.", "schedule_commitment"),
        ("The damage was our mistake.", "fault_admission"),
        ("We are withholding payment pending the dispute.", "dispute"),
        ("Our insurance claim has been filed.", "legal_or_insurance"),
        ("A worker fell from the scaffold this afternoon.", "safety_incident"),
        ("This is going in his file as a written warning.", "discipline"),
        ("We are terminating his employment.", "termination"),
        ("We will refund the deposit.", "refund_or_credit"),
        ("This is completely unacceptable.", "serious_complaint"),
        ("I think the second coat went on, but I'm not sure.", "uncertainty"),
    ],
)
def test_risk_categories_are_caught(text, category):
    result = screen(text, recipient_role=RecipientRole.GENERAL_CONTRACTOR)
    assert result.requires_approval
    assert category in result.categories


def test_a_routine_daily_log_passes_clean():
    body = (
        "Work Completed Today\n"
        "- Surface preparation completed in the second-floor corridor.\n"
        "- Primer application in progress in Room 204.\n\n"
        "Plan for the Next Working Day\n"
        "- First finish coat is planned, subject to site readiness.\n\n"
        "Safety\n"
        "No injuries or incidents were reported today.\n"
    )
    result = screen(body, recipient_role=RecipientRole.PROJECT_MANAGER)
    assert not result.requires_approval, result.categories


def test_negated_safety_language_is_not_an_incident():
    """'No accidents today' is a daily-log line, not a safety event."""
    clean = screen("No accidents and no injuries were reported.", recipient_role=RecipientRole.CLIENT)
    assert "safety_incident" not in clean.categories

    real = screen("There was an accident on the third floor.", recipient_role=RecipientRole.CLIENT)
    assert "safety_incident" in real.categories


def test_denying_responsibility_is_never_excused_by_negation():
    """Negation clears an occurrence, not a liability statement."""
    result = screen("We are not responsible for the damage.", recipient_role=RecipientRole.CLIENT)
    assert "fault_admission" in result.categories


def test_uncertainty_gates_external_but_not_internal():
    text = "I think the north corridor got primer, but I am not sure."

    external = screen(text, recipient_role=RecipientRole.GENERAL_CONTRACTOR)
    assert "uncertainty" in external.categories

    # Asking the crew a tentative question is the clarification workflow
    # working, not a risk to gate.
    internal = screen(text, recipient_role=RecipientRole.CREW_LEAD)
    assert "uncertainty" not in internal.categories
    assert not internal.requires_approval


def test_discipline_gates_even_for_an_internal_recipient():
    result = screen("This is a formal written warning.", recipient_role=RecipientRole.EMPLOYEE)
    assert result.requires_approval
    assert "discipline" in result.categories


def test_confidential_information_is_blocked_from_external_recipients():
    text = "Our margin on this floor is thin, so keep the crew moving."

    external = screen(text, recipient_role=RecipientRole.CLIENT)
    assert external.requires_approval
    assert "confidential_disclosure" in external.categories

    internal = screen(text, recipient_role=RecipientRole.MANAGEMENT)
    assert "confidential_disclosure" not in internal.categories


def test_access_codes_are_treated_as_disclosure():
    result = screen("The alarm code is on the panel by the door.", recipient_role=RecipientRole.SUBCONTRACTOR)
    assert "confidential_disclosure" in result.categories


def test_subject_line_is_screened_too():
    result = screen("See attached.", subject="Change order pricing", recipient_role=RecipientRole.CLIENT)
    assert result.requires_approval


def test_style_warnings_do_not_gate_but_are_reported():
    result = screen(
        "The other trades always leave a mess and their guys are lazy.",
        recipient_role=RecipientRole.CREW_LEAD,
    )
    assert not result.requires_approval
    assert result.style_warnings


def test_translation_disclosure_is_a_style_warning():
    result = screen(
        "Translated from the crew lead's Arabic voice note.",
        recipient_role=RecipientRole.CLIENT,
    )
    assert any("translation" in w.guidance.lower() for w in result.style_warnings)


def test_unknown_recipient_role_is_treated_as_external():
    """Unknown means careful, not safe."""
    result = screen("Our margin is thin.", recipient_role="someone_new")
    assert result.external
    assert result.requires_approval


def test_reason_names_the_trigger():
    result = screen("The change order will be $900.", recipient_role=RecipientRole.CLIENT)
    reason = result.reason()
    assert "pricing" in reason or "change_order" in reason
    assert "approval" in reason.lower()


def test_screen_record_reads_the_stored_body():
    record = {
        "body": "We guarantee completion by Friday.",
        "subject": "Update",
        "recipient_role": RecipientRole.CLIENT.value,
    }
    assert screen_record(record).requires_approval
