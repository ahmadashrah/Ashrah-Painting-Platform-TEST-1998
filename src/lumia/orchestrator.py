"""Routing and the standing operating cycles.

The daily loop and weekly review are the two recurring jobs from the spec.
Both are driven by a prompt that hands the agent pre-computed state, so it
spends its turns deciding and acting rather than re-deriving the numbers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from .agent import Agent, AgentRun
from .agents import ROLES, build_agent
from .llm import ClaudeClient
from .reporting import growth_review
from .tools import Toolbox
from .workspace import Workspace

#: Keyword hints for routing a free-form request to a specialist.
ROUTING_HINTS: dict[str, tuple[str, ...]] = {
    "research": (
        "research", "find", "prospect", "signal", "permit", "tender", "market",
        "who is", "look up", "identify", "score", "qualify", "list of",
    ),
    "outreach": (
        "email", "outreach", "reach out", "contact", "follow up", "follow-up",
        "message", "draft a note", "meeting", "call", "sequence", "introduce",
    ),
    "content": (
        "case study", "post", "linkedin", "capability statement", "content",
        "write up", "portfolio", "testimonial", "brochure", "article",
    ),
    "crm": (
        "crm", "clean up", "cleanup", "hygiene", "duplicate", "stage", "pipeline data",
        "next action", "tidy", "audit the records",
    ),
    "scheduler": (
        "crew", "shift", "roster", "staffing", "staff the", "assign", "schedule",
        "reschedule", "time off", "day off", "vacation", "overtime", "availability",
        "site", "phase", "deficienc", "walkthrough", "job site", "deadline",
        "clock in", "clock-in", "attendance",
        # Longer, more specific forms of phrases the research agent also
        # matches, so "who is on the crew" doesn't land on research.
        "who is on", "who's on", "who is working", "who's working", "working on",
    ),
}


@dataclass
class Orchestrator:
    workspace: Workspace
    toolbox: Toolbox
    client: ClaudeClient

    @classmethod
    def build(cls, workspace: Workspace, client: ClaudeClient | None = None) -> "Orchestrator":
        return cls(
            workspace=workspace,
            toolbox=Toolbox(workspace),
            client=client or ClaudeClient(workspace.settings),
        )

    def agent(self, role: str) -> Agent:
        return build_agent(role, self.workspace, self.toolbox, self.client)

    # --- routing --------------------------------------------------------

    def route(self, task: str) -> str:
        """Pick the specialist best matched to a free-form request.

        Falls back to the director, who owns allocation when the request
        doesn't obviously belong to one specialist.
        """
        text = task.lower()
        scores = {
            role: sum(1 for hint in hints if hint in text)
            for role, hints in ROUTING_HINTS.items()
        }
        best = max(scores, key=lambda role: scores[role])
        return best if scores[best] > 0 else "director"

    def handle(self, task: str, role: str | None = None) -> AgentRun:
        chosen = role or self.route(task)
        if chosen not in ROLES:
            raise ValueError(f"unknown role '{chosen}'; expected one of {', '.join(ROLES)}")
        return self.agent(chosen).run(task)

    # --- daily operating loop -------------------------------------------

    def daily_loop(self, focus: str = "") -> AgentRun:
        """Review → prioritize → research → plan → execute → record → learn."""
        state = {
            "pipeline": self.workspace.crm.pipeline(),
            "priority_accounts": self.toolbox.call("priority_accounts", {"limit": 10}),
            "open_are_records": self.workspace.memory.open_are_records()[:5],
            "pending_approvals": len(self.workspace.approvals.pending()),
        }

        task = f"""\
Run today's operating cycle.

Here is the current state, already computed — do not re-fetch it:

{json.dumps(state, indent=2, default=str)}

Work the cycle in order:

1. REVIEW — read the state above. Note overdue actions, unmanaged accounts,
   and anything stalled.
2. PRIORITIZE — pick the two or three highest-expected-value actions. Say why
   those and not others. Do not spread effort evenly.
3. RESEARCH — gather only the context those actions need.
4. PLAN — for each action: who, why now, which channel, what outcome you want.
5. EXECUTE — perform the actions you are authorized to perform. Anything that
   needs approval will be queued; say so rather than claiming it was done.
6. RECORD — every account you touched leaves with a correct stage, a next
   action and a date. Log real interactions only.
7. LEARN — close any ARE record where evidence has arrived, and record a
   lesson if one is genuinely supported.

{f'Additional focus for today: {focus}' if focus else ''}

Finish with a short report: what you did, what changed in the CRM, what is
waiting on a human, and the single most valuable thing to do next.
"""
        return self.agent("director").run(task, max_iterations=16)

    # --- scheduling cycle -------------------------------------------------

    def scheduling_cycle(self, start_date: str = "", focus: str = "") -> AgentRun:
        """The Scheduling Agent's standing cycle.

        State is pre-computed and handed over, for the same reason the daily
        growth loop does it: the agent should spend its turns deciding, not
        re-deriving numbers the engine already computed deterministically.
        """
        start = _parse_start(start_date)
        state = {
            "today": date.today().isoformat(),
            "planning_from": start.isoformat(),
            "risk_report": self.toolbox.call("scheduling_risk_report", {"horizon_days": 14}),
            "projects_by_priority": self.toolbox.call("list_projects", {"limit": 15}),
            "availability_today": self.toolbox.call(
                "crew_availability", {"work_date": date.today().isoformat()}
            ),
            "proposed_week": self.toolbox.call(
                "plan_seven_day_schedule", {"start_date": start.isoformat(), "days": 7}
            ),
            "pending_approvals": len(self.workspace.approvals.pending()),
            "integrations": self.workspace.status()["integrations"],
        }

        task = f"""\
Run the scheduling cycle.

Here is the current state, already computed from the operations records —
do not re-fetch it:

{json.dumps(state, indent=2, default=str)}

Work it in order:

1. REVIEW — read the risk report first. Note what is already broken:
   double-bookings, shifts on approved time off, unstaffed starts,
   undeliverable deadlines, budget overruns, expiring certifications.
2. GAPS — identify what information is missing, stale or contradictory. Say
   exactly what is missing rather than working around it silently.
3. RESOLVE — work the critical and high alerts in priority order. For each
   conflict: the cause, who and what it affects, the operational and
   financial impact, at least two options where they exist, your
   recommendation and the trade-offs.
4. SCHEDULE — turn the proposed week into real draft shifts for the work you
   are authorized to schedule. Verify availability and qualification through
   the tools before assigning anyone. Anything refused stays refused — report
   the uncovered days honestly rather than forcing a fit.
5. CONFIRM AND NOTIFY — confirm the shifts that are ready and brief those
   crews. Do not brief anyone on a draft.
6. ESCALATE — prepare, but do not execute, anything needing management
   approval. State the proposed action and the reasoning.
7. LEARN — close any ARE record where evidence has arrived, and record a
   lesson only where the evidence genuinely supports one.

{f'Additional focus: {focus}' if focus else ''}

Finish with a short report: the schedule you produced and its status
(draft or confirmed), the risks you could not resolve, what is waiting on a
human, your confidence level, and when the schedule should next be reviewed.
"""
        return self.agent("scheduler").run(task, max_iterations=18)

    def monitoring_pass(self, horizon_days: int = 14) -> AgentRun:
        """Proactive monitoring: what is going wrong, ranked, with an owner."""
        state = {
            "risk_report": self.toolbox.call("scheduling_risk_report", {"horizon_days": horizon_days}),
            "variance": self.toolbox.call("schedule_variance_report", {}),
        }
        task = f"""\
Produce the scheduling monitoring report.

These findings are computed from the operations records — treat them as
KNOWN FACT and do not recompute or contradict them:

{json.dumps(state, indent=2, default=str)}

For every warning worth raising, give: what happened, why it matters, which
projects and people it affects, the expected impact, the recommended
action, whether it needs approval, and the deadline for deciding. Rank them
critical, high, medium, low, and drop anything that does not earn a
manager's attention — an alert nobody acts on trains people to ignore the
next one.

Then read the variance data. Where estimated and actual hours diverge
consistently, say what the evidence supports changing — a duration
estimate, a crew size, a productivity assumption or a risk buffer — and file
it as an improvement proposal rather than applying it. Where the sample is
too small or actuals are unverified, say so and leave the assumption alone.

Label every conclusion KNOWN FACT, INFERENCE or UNKNOWN.
"""
        return self.agent("scheduler").run(task, max_iterations=10)

    # --- weekly growth review --------------------------------------------

    def weekly_review(self, days: int = 7) -> AgentRun:
        metrics = growth_review(self.workspace, days=days)
        task = f"""\
Produce the weekly growth review.

These metrics are computed from the CRM — treat them as KNOWN FACT and do
not recompute or contradict them:

{json.dumps(metrics, indent=2, default=str)}

Cover pipeline, activity, performance and intelligence. Then interpret:

- What actually moved, and what the evidence supports as the reason.
- Where a rate is null, say the denominator was zero rather than reporting
  it as zero performance.
- What failed, plainly. A failed experiment reported honestly is useful.
- Label each conclusion KNOWN FACT, INFERENCE or UNKNOWN.

End with the five highest-impact actions for next week, most valuable
first, each with the account or segment it applies to and why it beats the
alternatives. If the data shows part of the system itself is
underperforming, file an improvement proposal.
"""
        return self.agent("director").run(task, max_iterations=10)


def _parse_start(start_date: str) -> date:
    if not start_date:
        return date.today()
    try:
        return date.fromisoformat(start_date)
    except ValueError as exc:
        raise ValueError(f"start_date must be an ISO date, e.g. 2026-08-17 ({exc})") from exc
