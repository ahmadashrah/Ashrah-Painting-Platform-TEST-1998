"""Runs happen in phases, and a phase may only touch its own tools.

A run used to be one long conversation holding every tool its role owned,
from the first read to the last send. That works, and it has two problems
worth fixing.

**It is hard to watch.** "The client agent did eleven things" tells an
operator almost nothing. "It gathered, composed, reviewed, then sent" tells
them where it is and what should happen next.

**It is too permissive for too long.** An agent gathering facts has no
business holding `send_communication`, and one composing a daily log has no
business ordering paint. Narrowing the toolset to the phase means a
mis-selected tool is impossible rather than merely unlikely — and the
narrowing is enforced at dispatch, not just by omitting the schema.

Each phase declares what it is for and exactly what it may use. Phases run
in order inside one numbered run, sharing its budget and its kill switch,
and each one's outcome is logged. A phase hands the next its written
result, not its raw conversation: the composer receives the gathered facts
as findings, which is what a handoff is.

Adding a phase is adding an entry here. The tools it names are intersected
with the role's own allowlist, so a plan can narrow a role but never widen
it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Phase:
    """One stage of a run, and the only tools it may reach."""

    name: str
    goal: str
    tools: list[str] = field(default_factory=list)
    max_iterations: int = 4
    #: A phase that may find nothing to do and should not fail the run.
    optional: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "goal": self.goal, "tools": list(self.tools),
                "max_iterations": self.max_iterations}


#: Every role's common context tools — reading the record is allowed in any
#: phase, because a phase that cannot look things up guesses instead.
READING = ["recall_lessons", "screen_message"]


def _phase(name: str, goal: str, tools: list[str], turns: int = 4, optional: bool = True) -> Phase:
    return Phase(name=name, goal=goal, tools=sorted(set(tools + READING)),
                 max_iterations=turns, optional=optional)


#: The ordered plan for each agent. Names are deliberately verbs: a phase is
#: something the run *does*, not a category it belongs to.
PLANS: dict[str, list[Phase]] = {
    # --- communication ----------------------------------------------------
    "intake": [
        _phase("gather", "Find what the field submitted and what is still unprocessed.",
               ["list_projects", "get_project", "list_field_submissions", "list_project_media"]),
        _phase("transcribe", "Turn voice notes and photos into text. Never invent a transcript.",
               ["transcribe_field_submission", "describe_media"], turns=6),
        _phase("normalize", "Translate and clean each submission into professional English.",
               ["process_field_submission"], turns=6),
        _phase("verify", "Cross-check against scope, clock records and earlier reports. Caption photos.",
               ["verify_field_submission", "caption_media", "list_project_media"], turns=6),
        _phase("resolve", "Ask for what is missing; escalate what cannot wait.",
               ["raise_open_item", "close_open_item", "raise_escalation", "list_open_items"]),
    ],
    "client_comms": [
        _phase("gather", "Collect only verified material for this project and date.",
               ["list_projects", "get_project", "build_daily_log", "list_field_submissions",
                "list_project_media", "communication_history", "list_open_items"]),
        _phase("compose", "Write the log from the gathered facts and nothing else.",
               ["compose_daily_log"], turns=5),
        _phase("review", "Check the wording and pick the channel before anyone sees it.",
               ["recommend_channel", "communication_history"]),
        _phase("send", "Draft to each recipient and send what is allowed to send.",
               ["draft_communication", "send_communication"], turns=6),
        _phase("record", "Leave the project managed: open items, replies, escalations.",
               ["raise_open_item", "close_open_item", "log_communication_response",
                "record_communication_preference", "raise_escalation"]),
    ],
    "crew_comms": [
        _phase("gather", "Find the crews, the assignments and the outstanding questions.",
               ["list_projects", "get_project", "list_open_items", "list_field_submissions",
                "unanswered_communications", "communication_history"]),
        _phase("dispatch", "Write and send each crew what they need to start.",
               ["draft_communication", "send_communication", "recommend_channel"], turns=8),
        _phase("track", "Record what was asked and what is still owed.",
               ["raise_open_item", "close_open_item", "log_communication_response",
                "upsert_crew_member", "raise_escalation"]),
    ],
    "vendor_comms": [
        _phase("gather", "Confirm the project, the supplier and what is actually needed.",
               ["list_projects", "get_project", "communication_history", "list_open_items"]),
        _phase("prepare", "Prepare the request with verified product details only.",
               ["draft_communication", "place_material_order", "recommend_channel"], turns=6),
        _phase("confirm", "Send what may be sent, and track what was promised.",
               ["send_communication", "raise_open_item", "log_communication_response",
                "raise_escalation"]),
    ],
    "escalation": [
        _phase("gather", "Assemble the evidence before forming a view.",
               ["list_projects", "get_project", "list_escalations", "list_field_submissions",
                "list_project_media", "unanswered_communications", "communication_history"]),
        _phase("assess", "Separate what is verified from what is inferred.",
               ["communication_performance", "list_open_items"]),
        _phase("report", "Give management facts, impact, a recommendation and a draft response.",
               ["raise_escalation", "draft_communication", "propose_improvement", "record_lesson"],
               turns=5),
    ],
    # --- growth -------------------------------------------------------------
    "director": [
        _phase("review", "Read the pipeline and find the highest-value work.",
               ["priority_accounts", "pipeline_report", "get_account", "find_accounts",
                "build_account_brief", "list_contacts"]),
        _phase("act", "Do the two or three things worth doing, and say why those.",
               ["set_next_action", "advance_stage", "upsert_opportunity", "log_interaction"], turns=6),
        _phase("learn", "Close what has evidence; propose a change if the system is at fault.",
               ["open_are_record", "close_are_record", "record_lesson", "propose_improvement"]),
    ],
    "research": [
        _phase("search", "Look for signals worth acting on.",
               ["search_market_signals", "research_account", "find_accounts", "get_account"], turns=6),
        _phase("qualify", "Score what you found and check for an existing relationship.",
               ["score_account_tool", "get_relationship_history", "build_account_brief",
                "priority_accounts"]),
        _phase("record", "Turn signals into actions with owners and dates.",
               ["record_signal", "upsert_account", "upsert_contact", "set_next_action"], turns=6),
    ],
    "outreach": [
        _phase("prepare", "Read the relationship before writing a word.",
               ["build_account_brief", "get_relationship_history", "list_contacts", "get_account",
                "estimate_paint_job"]),
        _phase("write", "Draft something specific to this account.",
               ["save_content"], turns=4),
        _phase("send", "Send what is allowed; queue what is not.",
               ["send_followup_email", "send_first_contact_email", "schedule_meeting"], turns=5),
        _phase("record", "Log what happened and set the next action.",
               ["log_interaction", "set_next_action", "advance_stage"]),
    ],
    "content": [
        _phase("gather", "Collect authorized detail only.",
               ["build_account_brief", "get_account", "find_accounts", "pipeline_report",
                "list_contacts"]),
        _phase("write", "Build the asset with the customer outcome as the hero.",
               ["save_content"], turns=5),
        _phase("publish", "Publishing needs a human; submit it and say so.",
               ["publish_content"], turns=2),
    ],
    "crm": [
        _phase("audit", "Find what is stale, unmanaged or duplicated.",
               ["pipeline_report", "priority_accounts", "find_accounts", "get_account",
                "list_contacts", "build_account_brief"]),
        _phase("fix", "Leave every active account with a correct stage and a next action.",
               ["upsert_account", "upsert_contact", "set_next_action", "advance_stage",
                "upsert_opportunity", "log_interaction", "score_account_tool"], turns=8),
    ],
}


def plan_for(role: str, allowed: list[str] | None = None) -> list[Phase]:
    """The phases for a role, narrowed to what that role may actually use.

    A plan can restrict a role and never extend it: the intersection is
    taken against the role's own allowlist, so adding a tool to a phase
    that the role does not hold changes nothing.
    """
    phases = PLANS.get(role)
    if not phases:
        # An agent with no plan still runs — as a single phase holding its
        # whole toolset. A new role is unphased, never unable to run.
        return [Phase(name="execute", goal="Complete the task.",
                      tools=list(allowed or []), max_iterations=12, optional=False)]

    if allowed is None:
        return phases

    permitted = set(allowed)
    narrowed = []
    for phase in phases:
        tools = sorted(t for t in phase.tools if t in permitted)
        narrowed.append(
            Phase(name=phase.name, goal=phase.goal, tools=tools,
                  max_iterations=phase.max_iterations, optional=phase.optional)
        )
    return narrowed


def describe(role: str) -> list[dict[str, Any]]:
    return [phase.to_dict() for phase in PLANS.get(role, [])]
