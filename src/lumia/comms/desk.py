"""Routing and the standing communication cycles.

The day has a shape: field reports arrive and get processed, clients get
their daily log, crews get told where to be, unanswered questions get
chased, and the week gets reviewed. Each cycle hands the agent
pre-computed state so it spends its turns deciding and writing rather than
re-deriving what the store already knows.

This mirrors `orchestrator.Orchestrator` on the growth side, and stays
separate from it for the same reason the prompts are separate: routing
"email the client about tomorrow's access" and "email the prospect about a
tender" to the same set of specialists would serve neither well.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..agent import Agent, AgentRun
from ..agents import COMMS_ROLES, build_agent
from ..llm import ClaudeClient, build_client
from ..tools import Toolbox
from ..workspace import Workspace
from ..domain.projects import today_iso
from .reporting import communication_review

#: Keyword hints for routing a free-form request to a specialist.
ROUTING_HINTS: dict[str, tuple[str, ...]] = {
    "intake": (
        "submission", "field report", "transcribe", "translate", "voice",
        "photo", "picture", "caption", "arabic", "kurdish", "french",
        "what the crew sent", "clarify", "verify",
    ),
    "client_comms": (
        "daily log", "daily report", "client", "general contractor", "gc ",
        "superintendent", "project manager", "progress update", "update the",
        "site instruction", "schedule change",
    ),
    "crew_comms": (
        "crew", "employee", "painter", "foreman", "dispatch", "assign",
        "start time", "tell the guys", "worker", "staff",
    ),
    "vendor_comms": (
        "supplier", "vendor", "material", "delivery", "order", "paint order",
        "product code", "colour", "color", "sheen", "purchase order",
    ),
    "escalation": (
        "escalate", "incident", "injury", "damage", "dispute", "complaint",
        "delay", "change order", "management", "claim", "urgent problem",
    ),
}


@dataclass
class CommunicationDesk:
    workspace: Workspace
    toolbox: Toolbox
    client: ClaudeClient

    @classmethod
    def build(cls, workspace: Workspace, client: ClaudeClient | None = None) -> "CommunicationDesk":
        return cls(
            workspace=workspace,
            toolbox=Toolbox(workspace),
            client=client or build_client(workspace.settings),
        )

    def agent(self, role: str) -> Agent:
        return build_agent(role, self.workspace, self.toolbox, self.client)

    # --- routing -----------------------------------------------------------

    def route(self, task: str) -> str:
        """Pick the specialist best matched to a free-form request."""
        text = task.lower()
        scores = {
            role: sum(1 for hint in hints if hint in text)
            for role, hints in ROUTING_HINTS.items()
        }
        best = max(scores, key=lambda role: scores[role])
        # Client reporting is the default: most communication is outward.
        return best if scores[best] > 0 else "client_comms"

    def handle(self, task: str, role: str | None = None) -> AgentRun:
        chosen = role or self.route(task)
        if chosen not in COMMS_ROLES:
            raise ValueError(f"unknown role '{chosen}'; expected one of {', '.join(COMMS_ROLES)}")
        return self.agent(chosen).run(task)

    # --- intake ------------------------------------------------------------

    def intake(self, project_id: str = "", work_date: str = "") -> AgentRun:
        """Process the field submissions that are not yet usable."""
        on = work_date or today_iso()
        projects = (
            [self.workspace.comms.get_project(project_id)]
            if project_id
            else self.workspace.comms.active_projects()
        )
        state = []
        for project in [p for p in projects if p]:
            submissions = self.workspace.comms.submissions_for(project["id"], work_date=on)
            pending = [s for s in submissions if not s.get("processed") or s.get("conflicts")]
            if not pending:
                continue
            state.append(
                {
                    "project": {"id": project["id"], "name": project.get("name"), "scope": project.get("scope_summary"), "exclusions": project.get("exclusions")},
                    "submissions": pending,
                    "uncaptioned_media": self.toolbox.call("list_project_media", {"project_id": project["id"]}).get("uncaptioned", []),
                }
            )

        task = f"""\
Process today's field submissions ({on}).

Here is what is waiting, already fetched — do not re-fetch it:

{json.dumps(state, indent=2, default=str)}

For each submission:

1. TRANSCRIBE any voice recording verbatim.
2. TRANSLATE it faithfully into clear English if it did not arrive in English.
3. NORMALIZE it into professional English: fix grammar and organization,
   remove slang, emotion and blame, keep the worker's meaning intact. Record
   this with process_field_submission — the original stays untouched.
4. CAPTION every uncaptioned photo factually, and flag anything blurry,
   duplicated, unrelated, inappropriate or confidential.
5. READ THE CROSS-CHECK. Where it reports a conflict, do not pick a version:
   flag it, and raise an open item asking the employee one specific question.
   Where information is missing, ask for exactly that.
6. ESCALATE anything that meets the escalation criteria — injury, damage,
   out-of-scope work, a potential change order, a major delay.

Finish with: what you processed, what conflicts you found, what you asked
for, and what is ready for client reporting.
"""
        return self.agent("intake").run(task, max_iterations=16)

    # --- daily client reporting ---------------------------------------------

    def daily_logs(self, project_id: str = "", log_date: str = "") -> AgentRun:
        """Compose and send the client-facing daily log for each live project."""
        on = log_date or today_iso()
        projects = (
            [self.workspace.comms.get_project(project_id)]
            if project_id
            else self.workspace.comms.active_projects()
        )

        state = []
        for project in [p for p in projects if p]:
            facts = self.toolbox.call("build_daily_log", {"project_id": project["id"], "log_date": on})
            state.append(
                {
                    "facts": facts,
                    "recipients": [
                        {"id": c["id"], "name": c.get("name"), "role": c.get("role"), "channel": c.get("preferred_channel")}
                        for c in self.workspace.comms.daily_log_recipients(project["id"])
                    ],
                    "existing_log": self.workspace.comms.daily_log_on(project["id"], on),
                }
            )

        task = f"""\
Produce and send the daily client logs for {on}.

Here is the verified material for each project, already gathered — do not
re-fetch it:

{json.dumps(state, indent=2, default=str)}

For each project:

1. READ THE GAPS first. If a submission is conflicted or unprocessed, the log
   cannot go out — say so and ask for what you need instead.
2. COMPOSE the log with compose_daily_log, writing each bullet only from the
   verified material above. Neutral language on site conditions: explain
   impact without assigning blame. Name every decision or approval you need
   from the client under issues. Hedge the next-day plan.
3. DRAFT it to each recipient who receives the daily log, and SEND.
4. Where a send is held for approval, say so plainly. Do not describe it as
   sent.
5. RAISE an open item for anything you asked the client to decide, with a due
   date, so it can be chased.

Finish with: which logs went out, which are held and why, and what is now
waiting on the client.
"""
        return self.agent("client_comms").run(task, max_iterations=18)

    # --- crew dispatch --------------------------------------------------------

    def dispatch(self, project_id: str = "", work_date: str = "") -> AgentRun:
        """Tell each crew where to be and what to do."""
        on = work_date or today_iso()
        projects = (
            [self.workspace.comms.get_project(project_id)]
            if project_id
            else self.workspace.comms.active_projects()
        )
        state = [
            {
                "project": {
                    "id": p["id"],
                    "name": p.get("name"),
                    "address": p.get("address"),
                    "site_access_notes": p.get("site_access_notes"),
                    "scope_summary": p.get("scope_summary"),
                },
                "crew": self.workspace.comms.crew_for(p["id"]),
                "open_items": self.workspace.comms.open_items(p["id"]),
                "last_log": self.workspace.comms.previous_daily_log(p["id"], before=on),
            }
            for p in projects
            if p
        ]

        task = f"""\
Send the crew dispatch for {on}.

Project and crew state, already fetched:

{json.dumps(state, indent=2, default=str)}

For each crew member on each project, write a short, direct, respectful
message giving: the project and location, start time, the work assigned,
materials and equipment needed, the site contact, any safety or access
requirement, what to report back at the end of the day, and a request to
confirm.

Use plain language. Where an employee reports in another language, keep the
English simple and unambiguous. Do not invent a start time, an access
arrangement or an assignment that the state above does not support — ask
instead.

Finish with who was dispatched, who has not confirmed, and anything you had
to leave unanswered.
"""
        return self.agent("crew_comms").run(task, max_iterations=16)

    # --- follow-up sweep --------------------------------------------------------

    def followups(self) -> AgentRun:
        """Chase unanswered questions, overdue items and failed deliveries."""
        state = {
            "unanswered": self.toolbox.call("unanswered_communications", {}),
            "open_items": self.toolbox.call("list_open_items", {}),
            "open_escalations": self.workspace.comms.escalations(status="open"),
            "pending_approvals": self.workspace.approvals.pending(),
        }

        task = f"""\
Run the follow-up sweep.

Current state, already fetched:

{json.dumps(state, indent=2, default=str)}

1. FAILED DELIVERIES first. A failed delivery is not a sent message — re-send
   on another channel, or escalate if it keeps failing.
2. OVERDUE ITEMS. Chase each one with a short, specific message to whoever
   owes the answer. Reference the original request rather than restating it.
3. UNANSWERED REQUESTS that are not yet overdue: decide whether a nudge is
   warranted or whether it is simply not due yet. Do not chase for the sake
   of activity.
4. OPEN ESCALATIONS: say what is still waiting on management.
5. PENDING APPROVALS: list what is queued and what it is blocking.

Finish with what you chased, what you deliberately left alone, and what
needs a human.
"""
        return self.agent("crew_comms").run(task, max_iterations=14)

    # --- performance review -------------------------------------------------------

    def review(self, days: int = 7) -> AgentRun:
        metrics = communication_review(self.workspace, days=days)
        task = f"""\
Produce the communication performance review.

These metrics are computed from the record — treat them as KNOWN FACT and do
not recompute or contradict them:

{json.dumps(metrics, indent=2, default=str)}

Cover reporting, accuracy, delivery, responsiveness and the safety net. Then
interpret:

- What actually improved or degraded, and what the evidence supports as the
  reason.
- Where a rate is null, say the denominator was zero rather than reporting it
  as zero performance.
- What went wrong, plainly. A missed log or a wrong recipient reported
  honestly is more useful than a clean-looking summary.
- Label each conclusion KNOWN FACT, INFERENCE or UNKNOWN.

End with concrete recommendations to improve field reporting forms, required
photo lists, voice-report questions, templates, approval workflows, client
timing or employee training. Recommendations are proposals until management
approves them — if the evidence shows part of the system itself is
underperforming, file an improvement proposal.
"""
        return self.agent("escalation").run(task, max_iterations=12)
