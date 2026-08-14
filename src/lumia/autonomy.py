"""Autonomy levels and the human-approval gate.

The operating spec defines three levels. This module is the enforcement
point: every tool is classified, and anything landing at Level 3 is parked
in an approval queue instead of executing. Nothing here is advisory — the
agent loop physically cannot run a Level 3 tool without a recorded
approval.

Escalation is dynamic as well as static. A tool that is normally Level 2
becomes Level 3 when the *target* raises the stakes: first contact with a
strategic account, or any outbound touch on a high-value relationship.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Any

from .comms.screening import screen_record
from .domain.accounts import new_id
from .domain.projects import CommKind


class AutonomyLevel(IntEnum):
    AUTONOMOUS = 1           # execute freely
    CONTROLLED = 2           # execute within approved rules and templates
    APPROVAL_REQUIRED = 3    # never executes without a human


#: Static classification. Anything not listed defaults to APPROVAL_REQUIRED —
#: an unknown tool is treated as dangerous, not as safe.
TOOL_LEVELS: dict[str, AutonomyLevel] = {
    # Level 1 — research, analysis, internal state.
    "search_market_signals": AutonomyLevel.AUTONOMOUS,
    "research_account": AutonomyLevel.AUTONOMOUS,
    "get_account": AutonomyLevel.AUTONOMOUS,
    "find_accounts": AutonomyLevel.AUTONOMOUS,
    "list_contacts": AutonomyLevel.AUTONOMOUS,
    "get_relationship_history": AutonomyLevel.AUTONOMOUS,
    "build_account_brief": AutonomyLevel.AUTONOMOUS,
    "score_account_tool": AutonomyLevel.AUTONOMOUS,
    "priority_accounts": AutonomyLevel.AUTONOMOUS,
    "pipeline_report": AutonomyLevel.AUTONOMOUS,
    "save_content": AutonomyLevel.AUTONOMOUS,
    "weekly_growth_review": AutonomyLevel.AUTONOMOUS,
    "draft_outreach": AutonomyLevel.AUTONOMOUS,
    "draft_case_study": AutonomyLevel.AUTONOMOUS,
    "build_meeting_brief": AutonomyLevel.AUTONOMOUS,
    "estimate_paint_job": AutonomyLevel.AUTONOMOUS,
    "recall_lessons": AutonomyLevel.AUTONOMOUS,
    "record_lesson": AutonomyLevel.AUTONOMOUS,
    "open_are_record": AutonomyLevel.AUTONOMOUS,
    "close_are_record": AutonomyLevel.AUTONOMOUS,
    "record_signal": AutonomyLevel.AUTONOMOUS,
    "propose_improvement": AutonomyLevel.AUTONOMOUS,
    # Level 2 — writes to the record of truth and routine, templated sends.
    "upsert_account": AutonomyLevel.CONTROLLED,
    "upsert_contact": AutonomyLevel.CONTROLLED,
    "log_interaction": AutonomyLevel.CONTROLLED,
    "set_next_action": AutonomyLevel.CONTROLLED,
    "advance_stage": AutonomyLevel.CONTROLLED,
    "upsert_opportunity": AutonomyLevel.CONTROLLED,
    "send_followup_email": AutonomyLevel.CONTROLLED,
    "schedule_meeting": AutonomyLevel.CONTROLLED,
    # Level 3 — anything that speaks to the market for the first time.
    "send_first_contact_email": AutonomyLevel.APPROVAL_REQUIRED,
    "send_pricing_commitment": AutonomyLevel.APPROVAL_REQUIRED,
    "publish_content": AutonomyLevel.APPROVAL_REQUIRED,

    # --- project communication -------------------------------------------
    # Level 1 — reading, intake, processing and preparing. The whole
    # transcribe → translate → verify → draft pipeline runs freely; none of
    # it reaches a recipient.
    "list_projects": AutonomyLevel.AUTONOMOUS,
    "get_project": AutonomyLevel.AUTONOMOUS,
    "record_field_submission": AutonomyLevel.AUTONOMOUS,
    "process_field_submission": AutonomyLevel.AUTONOMOUS,
    "verify_field_submission": AutonomyLevel.AUTONOMOUS,
    # Reading a recording or a photo is analysis, not action.
    "transcribe_field_submission": AutonomyLevel.AUTONOMOUS,
    "describe_media": AutonomyLevel.AUTONOMOUS,
    "list_field_submissions": AutonomyLevel.AUTONOMOUS,
    "add_project_media": AutonomyLevel.AUTONOMOUS,
    "caption_media": AutonomyLevel.AUTONOMOUS,
    "list_project_media": AutonomyLevel.AUTONOMOUS,
    "build_daily_log": AutonomyLevel.AUTONOMOUS,
    "compose_daily_log": AutonomyLevel.AUTONOMOUS,
    "draft_communication": AutonomyLevel.AUTONOMOUS,
    "screen_message": AutonomyLevel.AUTONOMOUS,
    "recommend_channel": AutonomyLevel.AUTONOMOUS,
    "communication_history": AutonomyLevel.AUTONOMOUS,
    "unanswered_communications": AutonomyLevel.AUTONOMOUS,
    "list_open_items": AutonomyLevel.AUTONOMOUS,
    "list_escalations": AutonomyLevel.AUTONOMOUS,
    "record_communication_preference": AutonomyLevel.AUTONOMOUS,
    "communication_performance": AutonomyLevel.AUTONOMOUS,
    # Level 2 — writes to the project record, and internal notifications.
    # Escalation sits here deliberately: telling a manager about an injury
    # or a dispute is the action the spec requires immediately, and queuing
    # it behind an approval would defeat the purpose.
    "upsert_project": AutonomyLevel.CONTROLLED,
    "upsert_project_contact": AutonomyLevel.CONTROLLED,
    "upsert_crew_member": AutonomyLevel.CONTROLLED,
    "log_communication_response": AutonomyLevel.CONTROLLED,
    "raise_open_item": AutonomyLevel.CONTROLLED,
    "close_open_item": AutonomyLevel.CONTROLLED,
    "raise_escalation": AutonomyLevel.CONTROLLED,
    # Dynamic — resolved per call against the stored draft. See _classify_send.
    "send_communication": AutonomyLevel.CONTROLLED,
    # Level 3 — committing Ashrah to a purchase.
    "place_material_order": AutonomyLevel.APPROVAL_REQUIRED,
}

#: Outbound tools whose level depends on who is being contacted.
OUTBOUND_TOOLS = {"send_followup_email", "send_first_contact_email", "publish_content"}

#: Accounts worth at least this much per year get human eyes on any outbound.
HIGH_VALUE_THRESHOLD = 150_000.0

#: The spec's Level 2 list for project communication: the message kinds
#: management has approved as routine and repeatable. Everything outside this
#: set requires a human, whatever it says — and everything inside it still has
#: to survive content screening.
AUTO_SENDABLE_KINDS = {
    CommKind.DAILY_LOG.value,
    CommKind.SCHEDULE_REMINDER.value,
    CommKind.ARRIVAL_NOTICE.value,
    CommKind.CONFIRMATION_REQUEST.value,
    CommKind.CLARIFICATION_REQUEST.value,
    CommKind.CREW_DISPATCH.value,
}


@dataclass
class ApprovalRequest:
    tool: str
    arguments: dict[str, Any]
    reason: str
    agent: str = ""
    account_id: str = ""
    project_id: str = ""
    #: Which run raised this, so an approval can be traced to its job.
    run_ref: str = ""
    status: str = "pending"      # pending | approved | rejected | executed
    decided_by: str = ""
    decision_note: str = ""
    id: str = field(default_factory=lambda: new_id("appr"))
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApprovalQueue:
    """File-backed queue of Level 3 actions awaiting a human."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write([])

    def _read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _write(self, data: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def submit(self, request: ApprovalRequest) -> ApprovalRequest:
        with self._lock:
            data = self._read()
            data.append(request.to_dict())
            self._write(data)
        return request

    def pending(self) -> list[dict[str, Any]]:
        return [r for r in self._read() if r.get("status") == "pending"]

    def all(self) -> list[dict[str, Any]]:
        return self._read()

    def decide(self, request_id: str, approved: bool, note: str = "", by: str = "human") -> dict[str, Any]:
        with self._lock:
            data = self._read()
            for record in data:
                if record.get("id") == request_id:
                    if record.get("status") != "pending":
                        return {"error": f"{request_id} is already {record.get('status')}"}
                    record["status"] = "approved" if approved else "rejected"
                    record["decided_by"] = by
                    record["decision_note"] = note
                    self._write(data)
                    return record
            return {"error": f"no approval request with id {request_id}"}

    def mark_executed(self, request_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            for record in data:
                if record.get("id") == request_id:
                    record["status"] = "executed"
                    record["result"] = result
                    self._write(data)
                    return


def classify(
    tool_name: str,
    arguments: dict[str, Any],
    account: dict[str, Any] | None = None,
    draft: dict[str, Any] | None = None,
) -> tuple[AutonomyLevel, str]:
    """Return the effective autonomy level for one call, plus the reason.

    `account` is the target account record when the tool acts on one, and
    `draft` is the stored communication when the tool sends one. Both are
    what make escalation possible: the level depends on who is being
    contacted and what the message actually says, not only on which tool
    was reached for.
    """
    base = TOOL_LEVELS.get(tool_name, AutonomyLevel.APPROVAL_REQUIRED)
    if tool_name not in TOOL_LEVELS:
        return base, f"'{tool_name}' is not in the autonomy table; unknown tools require approval."

    if tool_name == "send_communication":
        return _classify_send(draft)

    if tool_name not in OUTBOUND_TOOLS or account is None:
        return base, f"'{tool_name}' is classified Level {int(base)}."

    # Dynamic escalation on outbound communication.
    stage = str(account.get("stage", ""))
    tier = str(account.get("tier", ""))
    value = float(account.get("estimated_annual_value", 0) or 0)

    if tier == "A" and stage in {"prospect", "researched"}:
        return (
            AutonomyLevel.APPROVAL_REQUIRED,
            "First contact with a Tier A strategic account — the spec requires human approval.",
        )
    if value >= HIGH_VALUE_THRESHOLD:
        return (
            AutonomyLevel.APPROVAL_REQUIRED,
            f"Account is valued at ${value:,.0f}/yr, above the ${HIGH_VALUE_THRESHOLD:,.0f} "
            "high-value threshold for autonomous outbound.",
        )
    return base, f"'{tool_name}' is classified Level {int(base)} for this target."


def _classify_send(draft: dict[str, Any] | None) -> tuple[AutonomyLevel, str]:
    """Decide whether one outbound project message may send without a human.

    Three gates, in order, and all three must pass:

    1. The draft has to exist and name a recipient the platform resolved.
       An unreadable or unverified draft is never sent automatically.
    2. Its kind has to be on the spec's Level 2 list — routine progress
       reports, schedule reminders, arrival notices, confirmation and
       clarification requests, crew dispatches. Anything else is Level 3
       before its content is even read.
    3. Its *stored* body has to survive content screening. The body is read
       from the record rather than from the call's arguments, so a message
       cannot be screened as one thing and sent as another.
    """
    if draft is None:
        return (
            AutonomyLevel.APPROVAL_REQUIRED,
            "The draft could not be read, so its content cannot be screened. "
            "An unverifiable message is never sent automatically.",
        )

    if not draft.get("recipient_verified"):
        return (
            AutonomyLevel.APPROVAL_REQUIRED,
            "The recipient was not resolved against the project's recorded contacts.",
        )

    kind = str(draft.get("kind", ""))
    if kind not in AUTO_SENDABLE_KINDS:
        return (
            AutonomyLevel.APPROVAL_REQUIRED,
            f"A '{kind or 'unclassified'}' message is not on the list of routine, repeatable "
            "communications management has approved for automatic sending.",
        )

    result = screen_record(draft)
    if result.requires_approval:
        return AutonomyLevel.APPROVAL_REQUIRED, result.reason()

    return (
        AutonomyLevel.CONTROLLED,
        f"A '{kind}' message to {draft.get('recipient_name') or 'a recorded contact'} with no "
        "approval triggers in its content — Level 2, sends within approved rules.",
    )
