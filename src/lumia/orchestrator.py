"""Routing and the standing operating cycles.

The daily loop and weekly review are the two recurring jobs from the spec.
Both are driven by a prompt that hands the agent pre-computed state, so it
spends its turns deciding and acting rather than re-deriving the numbers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .agent import Agent, AgentRun
from .agents import ROLES, build_agent
from .llm import ClaudeClient, build_client
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
            client=client or build_client(workspace.settings),
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
