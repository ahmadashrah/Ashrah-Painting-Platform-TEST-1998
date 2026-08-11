"""The autonomy gate is the safety-critical part of the harness."""

from __future__ import annotations

from lumia.autonomy import ApprovalQueue, ApprovalRequest, AutonomyLevel, classify


def test_research_tools_are_autonomous():
    level, _ = classify("search_market_signals", {})
    assert level is AutonomyLevel.AUTONOMOUS


def test_crm_writes_are_controlled():
    level, _ = classify("log_interaction", {"account_id": "acct_1"})
    assert level is AutonomyLevel.CONTROLLED


def test_first_contact_always_needs_approval():
    level, _ = classify("send_first_contact_email", {"account_id": "acct_1"})
    assert level is AutonomyLevel.APPROVAL_REQUIRED


def test_unknown_tool_defaults_to_approval_required():
    """An unrecognized tool must be treated as dangerous, not as safe."""
    level, reason = classify("wire_funds_somewhere", {})
    assert level is AutonomyLevel.APPROVAL_REQUIRED
    assert "not in the autonomy table" in reason


def test_followup_escalates_on_first_contact_with_tier_a():
    account = {"tier": "A", "stage": "researched", "estimated_annual_value": 10_000}
    level, reason = classify("send_followup_email", {"account_id": "acct_1"}, account)
    assert level is AutonomyLevel.APPROVAL_REQUIRED
    assert "Tier A" in reason


def test_followup_escalates_on_high_value_account():
    account = {"tier": "B", "stage": "engaged", "estimated_annual_value": 200_000}
    level, reason = classify("send_followup_email", {"account_id": "acct_1"}, account)
    assert level is AutonomyLevel.APPROVAL_REQUIRED
    assert "high-value" in reason


def test_followup_stays_controlled_for_ordinary_warm_account():
    account = {"tier": "B", "stage": "engaged", "estimated_annual_value": 40_000}
    level, _ = classify("send_followup_email", {"account_id": "acct_1"}, account)
    assert level is AutonomyLevel.CONTROLLED


def test_approval_queue_round_trip(tmp_path):
    queue = ApprovalQueue(tmp_path / "approvals.json")
    request = queue.submit(
        ApprovalRequest(tool="send_first_contact_email", arguments={"to": "a@example.com"}, reason="test")
    )

    assert len(queue.pending()) == 1

    decided = queue.decide(request.id, approved=True, note="ok")
    assert decided["status"] == "approved"
    assert queue.pending() == []

    # A decision cannot be replayed.
    again = queue.decide(request.id, approved=False)
    assert "error" in again


def test_approval_queue_rejects_unknown_id(tmp_path):
    queue = ApprovalQueue(tmp_path / "approvals.json")
    assert "error" in queue.decide("appr_nope", approved=True)
