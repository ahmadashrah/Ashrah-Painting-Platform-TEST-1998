"""Communication performance — the numbers.

The evaluation section of the spec names the measures. They are computed
here rather than narrated, so "we are getting better at this" is a claim
with a denominator behind it.

Rates return `None` when nothing happened, never `0.0`. A delivery success
rate of zero means every message failed; a delivery success rate of `None`
means none were sent, and reporting the second as the first would be a
lie told with arithmetic.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ..domain.projects import DeliveryStatus, Verification

#: Tools whose gated calls count as an unsafe automatic send prevented.
GATED_COMMUNICATION_TOOLS = {"send_communication", "place_material_order"}


def communication_review(ws: Any, days: int = 7) -> dict[str, Any]:
    """Delivery, accuracy, responsiveness and prevention metrics for the period."""
    since = (date.today() - timedelta(days=days)).isoformat()

    projects = ws.comms.list_projects()
    active = [p for p in projects if p.get("live")]
    submissions = [
        s for s in ws.store.list("submissions") if str(s.get("work_date", "")) >= since
    ]
    logs = [
        log for log in ws.store.list("daily_logs") if str(log.get("log_date", "")) >= since
    ]
    communications = [
        c for c in ws.store.list("communications") if str(c.get("created_at", ""))[:10] >= since
    ]
    escalations = [
        e for e in ws.store.list("escalations") if str(e.get("raised_on", "")) >= since
    ]
    items = ws.store.list("open_items")

    sent = [c for c in communications if c.get("status") in {DeliveryStatus.SENT.value, DeliveryStatus.REPLIED.value}]
    failed = [c for c in communications if c.get("status") == DeliveryStatus.FAILED.value]
    gated = [c for c in communications if c.get("status") == DeliveryStatus.AWAITING_APPROVAL.value]
    corrections = [c for c in sent if str(c.get("corrects", "")).strip()]

    needs_translation = [
        s for s in submissions if str(s.get("original_language", "unknown")) not in {"en", "unknown"}
    ]
    translated = [s for s in needs_translation if str(s.get("translation_en", "")).strip()]
    with_missing = [s for s in submissions if s.get("missing")]
    conflicted = [s for s in submissions if s.get("verification") == Verification.CONFLICTED.value]

    daily_logs_sent = [log for log in logs if log.get("status") == "sent"]
    on_time = _on_time_logs(ws, daily_logs_sent)

    prevented = [
        request
        for request in ws.approvals.all()
        if request.get("tool") in GATED_COMMUNICATION_TOOLS
    ]

    responses = [
        (c, c["responses"][0])
        for c in communications
        if c.get("responses") and c.get("sent_at")
    ]
    sentiments = _count([r for _, r in responses], "sentiment")

    expected_logs = len(active) * days

    return {
        "period_days": days,
        "since": since,
        "volume": {
            "active_projects": len(active),
            "field_submissions": len(submissions),
            "daily_logs_composed": len(logs),
            "daily_logs_sent": len(daily_logs_sent),
            "messages_drafted": len(communications),
            "messages_sent": len(sent),
            "escalations_raised": len(escalations),
        },
        "reporting": {
            # An estimate: one log per active project per day in the period.
            "expected_daily_logs": expected_logs,
            "daily_log_coverage": _rate(len(daily_logs_sent), expected_logs),
            "on_time_rate": _rate(on_time, len(daily_logs_sent)),
            "field_reporting_compliance": _rate(
                len({s.get("project_id") for s in submissions}), len(active)
            ),
        },
        "accuracy": {
            "translation_coverage": _rate(len(translated), len(needs_translation)),
            "submissions_needing_translation": len(needs_translation),
            # Stated alongside the rates below, because a submission nobody has
            # processed yet contributes no missing information and no conflicts.
            # Without this number, an untouched backlog reads as a clean week.
            "pending_processing": len([s for s in submissions if not s.get("processed")]),
            "missing_information_rate": _rate(len(with_missing), len(submissions)),
            "conflict_rate": _rate(len(conflicted), len(submissions)),
            "correction_rate": _rate(len(corrections), len(sent)),
        },
        "delivery": {
            "delivery_success_rate": _rate(len(sent), len(sent) + len(failed)),
            "failed_deliveries": len(failed),
            "verified_recipient_rate": _rate(
                len([c for c in sent if c.get("recipient_verified")]), len(sent)
            ),
            "by_channel": _count(sent, "channel"),
            "by_kind": _count(sent, "kind"),
        },
        "responsiveness": {
            "replies_received": len(responses),
            "reply_rate": _rate(len(responses), len([c for c in sent if c.get("requires_response")])),
            "median_response_days": _median_response_days(responses),
            "response_sentiment": sentiments,
            "open_items": len([i for i in items if i.get("status") == "open"]),
            "overdue_items": len(
                [
                    i
                    for i in items
                    if i.get("status") == "open"
                    and i.get("due_date")
                    and str(i["due_date"]) < date.today().isoformat()
                ]
            ),
            "awaiting_reply": len(ws.comms.unanswered()),
        },
        "safety_net": {
            "unsafe_automatic_sends_prevented": len(prevented),
            "still_awaiting_approval": len([p for p in prevented if p.get("status") == "pending"]),
            "drafts_held_for_approval": len(gated),
            "open_escalations": len(ws.comms.escalations(status="open")),
        },
    }


def _on_time_logs(ws: Any, logs: list[dict[str, Any]]) -> int:
    """Logs whose message went out by the time the client expects it."""
    on_time = 0
    for log in logs:
        project = ws.comms.get_project(str(log.get("project_id", "")))
        expected = str((project or {}).get("daily_log_time", "")).strip()
        communication = ws.comms.get_communication(str(log.get("communication_id", "")))
        sent_at = str((communication or {}).get("sent_at", ""))
        if not sent_at:
            continue
        if not expected:
            # No stated deadline means it cannot be late.
            on_time += 1
            continue
        sent_time = sent_at[11:16]
        if sent_at[:10] == str(log.get("log_date")) and sent_time <= expected:
            on_time += 1
    return on_time


def _median_response_days(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> float | None:
    gaps = []
    for communication, response in pairs:
        try:
            sent = datetime.fromisoformat(str(communication["sent_at"])).date()
            replied = date.fromisoformat(str(response.get("received_on", "")))
        except (KeyError, ValueError):
            continue
        gaps.append((replied - sent).days)
    if not gaps:
        return None
    gaps.sort()
    middle = len(gaps) // 2
    if len(gaps) % 2:
        return float(gaps[middle])
    return round((gaps[middle - 1] + gaps[middle]) / 2, 1)


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def _count(records: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get(field, "unknown"))
        counts[key] = counts.get(key, 0) + 1
    return counts
