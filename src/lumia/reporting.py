"""Weekly growth review — the numbers.

Metrics are computed in code, not narrated by the model, so week-over-week
figures are comparable and can't drift. The agent's job is to interpret
them and recommend the next week's five highest-impact actions.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .domain.accounts import PipelineStage
from .workspace import Workspace

POSITIVE_OUTCOMES = {
    "positive_reply",
    "meeting_booked",
    "estimate_requested",
    "tender_invitation",
    "vendor_registration",
    "referral",
    "won",
}


def growth_review(ws: Workspace, days: int = 7) -> dict[str, Any]:
    """Activity, pipeline, conversion and intelligence for the period."""
    since = (date.today() - timedelta(days=days)).isoformat()

    accounts = ws.store.list("accounts")
    contacts = ws.store.list("contacts")
    interactions = [i for i in ws.store.list("interactions") if str(i.get("occurred_on", "")) >= since]
    opportunities = ws.store.list("opportunities")
    signals = [s for s in ws.store.list("signals") if str(s.get("observed_on", "")) >= since]
    are_records = ws.store.list("are_records")
    content = ws.store.list("content")

    outbound = [i for i in interactions if i.get("direction") == "outbound"]
    inbound = [i for i in interactions if i.get("direction") == "inbound"]
    positive = [i for i in interactions if i.get("outcome") in POSITIVE_OUTCOMES]
    meetings = [i for i in interactions if i.get("outcome") == "meeting_booked"]
    estimates = [i for i in interactions if i.get("outcome") == "estimate_requested"]
    tenders = [i for i in interactions if i.get("outcome") == "tender_invitation"]

    open_opps = [o for o in opportunities if o.get("stage") not in {"won", "lost"}]
    won = [o for o in opportunities if o.get("stage") == PipelineStage.WON.value]

    pipeline_value = sum(float(o.get("estimated_value", 0) or 0) for o in open_opps)
    weighted = sum(
        float(o.get("estimated_value", 0) or 0) * float(o.get("probability", 0) or 0) for o in open_opps
    )
    won_value = sum(float(o.get("estimated_value", 0) or 0) for o in won)

    return {
        "period_days": days,
        "since": since,
        "activity": {
            "accounts_total": len(accounts),
            "accounts_added": len([a for a in accounts if str(a.get("created_at", ""))[:10] >= since]),
            "decision_makers_known": len([c for c in contacts if c.get("is_decision_maker")]),
            "outreach_sent": len(outbound),
            "inbound_received": len(inbound),
            "signals_recorded": len(signals),
            "actionable_signals": len([s for s in signals if s.get("actionable")]),
            "content_drafted": len([c for c in content if str(c.get("created_at", ""))[:10] >= since]),
        },
        "pipeline": {
            "by_stage": _count(accounts, "stage"),
            "by_tier": _count(accounts, "tier"),
            "open_opportunities": len(open_opps),
            "pipeline_value": round(pipeline_value, 2),
            "weighted_pipeline_value": round(weighted, 2),
            "won_opportunities": len(won),
            "won_value": round(won_value, 2),
        },
        "conversion": {
            "reply_rate": _rate(len(inbound), len(outbound)),
            "positive_reply_rate": _rate(len(positive), len(outbound)),
            "meeting_rate": _rate(len(meetings), len(outbound)),
            "estimate_request_rate": _rate(len(estimates), len(outbound)),
            "tender_invitations": len(tenders),
        },
        "hygiene": {
            "unmanaged_accounts": len(
                [
                    a
                    for a in accounts
                    if a.get("stage") not in {"won", "lost", "nurture"}
                    and not str(a.get("next_action", "")).strip()
                ]
            ),
            "overdue_actions": len(
                [
                    a
                    for a in accounts
                    if a.get("next_action_date") and str(a["next_action_date"]) < date.today().isoformat()
                ]
            ),
            "open_are_records": len([r for r in are_records if not r.get("closed")]),
            "pending_approvals": len(ws.approvals.pending()),
        },
        "learning": {
            "lessons_total": len(ws.store.list("lessons")),
            "high_strength_lessons": len(
                [lesson for lesson in ws.store.list("lessons") if lesson.get("strength") == "high"]
            ),
            "open_improvement_proposals": len(ws.memory.proposals("proposed")),
        },
    }


def _rate(numerator: int, denominator: int) -> float | None:
    """None rather than 0.0 when there's no denominator — an unknown rate is
    not a zero rate, and reporting it as one hides that nothing was sent."""
    if denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def _count(records: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get(field, "unknown"))
        counts[key] = counts.get(key, 0) + 1
    return counts
