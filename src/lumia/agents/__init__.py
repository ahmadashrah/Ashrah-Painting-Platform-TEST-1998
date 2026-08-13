"""Lumia's specialist agents.

Each role gets a prompt addendum plus a restricted toolset. Restricting
tools is not decoration: a smaller, well-matched surface measurably
improves tool selection, and it keeps the Content agent from quietly
emailing a prospect.
"""

from __future__ import annotations

from ..agent import Agent
from ..llm import ClaudeClient
from ..tools import Toolbox
from ..workspace import Workspace

#: Read-only and memory tools every role can use.
COMMON = [
    "find_accounts",
    "get_account",
    "get_relationship_history",
    "build_account_brief",
    "recall_lessons",
    "open_are_record",
    "close_are_record",
    "record_lesson",
]

ROLE_TOOLS: dict[str, list[str]] = {
    "director": COMMON
    + [
        "priority_accounts",
        "pipeline_report",
        "list_contacts",
        "set_next_action",
        "advance_stage",
        "upsert_opportunity",
        "propose_improvement",
        "log_interaction",
    ],
    "research": COMMON
    + [
        "search_market_signals",
        "research_account",
        "record_signal",
        "score_account_tool",
        "priority_accounts",
        "upsert_account",
        "upsert_contact",
        "list_contacts",
        "set_next_action",
    ],
    "outreach": COMMON
    + [
        "list_contacts",
        "send_followup_email",
        "send_first_contact_email",
        "schedule_meeting",
        "log_interaction",
        "set_next_action",
        "advance_stage",
        "save_content",
        "estimate_paint_job",
    ],
    "content": COMMON
    + [
        "save_content",
        "publish_content",
        "list_contacts",
        "pipeline_report",
    ],
    "crm": COMMON
    + [
        "priority_accounts",
        "pipeline_report",
        "list_contacts",
        "upsert_account",
        "upsert_contact",
        "log_interaction",
        "set_next_action",
        "advance_stage",
        "upsert_opportunity",
        "score_account_tool",
    ],
}

#: Read-only project context every communication role needs.
COMMS_COMMON = [
    "list_projects",
    "get_project",
    "communication_history",
    "list_open_items",
    "raise_escalation",
    "screen_message",
    "recall_lessons",
    "open_are_record",
    "close_are_record",
]

#: The communication roles. Restriction does real work here: the Client
#: Reporting agent cannot text a crew lead and the Crew agent cannot email a
#: general contractor, because neither holds the other's contacts through a
#: tool it can reach.
COMMS_ROLE_TOOLS: dict[str, list[str]] = {
    "intake": COMMS_COMMON
    + [
        "record_field_submission",
        "process_field_submission",
        "verify_field_submission",
        "transcribe_field_submission",
        "describe_media",
        "list_field_submissions",
        "add_project_media",
        "caption_media",
        "list_project_media",
        "raise_open_item",
        "close_open_item",
        "upsert_crew_member",
    ],
    "client_comms": COMMS_COMMON
    + [
        "build_daily_log",
        "compose_daily_log",
        "list_project_media",
        "list_field_submissions",
        "draft_communication",
        "send_communication",
        "recommend_channel",
        "log_communication_response",
        "unanswered_communications",
        "raise_open_item",
        "close_open_item",
        "record_communication_preference",
        "upsert_project_contact",
    ],
    "crew_comms": COMMS_COMMON
    + [
        "draft_communication",
        "send_communication",
        "recommend_channel",
        "raise_open_item",
        "close_open_item",
        "log_communication_response",
        "list_field_submissions",
        "upsert_crew_member",
    ],
    "vendor_comms": COMMS_COMMON
    + [
        "draft_communication",
        "send_communication",
        "place_material_order",
        "recommend_channel",
        "log_communication_response",
        "raise_open_item",
        "upsert_project_contact",
    ],
    "escalation": COMMS_COMMON
    + [
        "list_escalations",
        "list_field_submissions",
        "list_project_media",
        "unanswered_communications",
        "draft_communication",
        "communication_performance",
        "propose_improvement",
        "record_lesson",
    ],
}

ROLE_TOOLS.update(COMMS_ROLE_TOOLS)

ROLES = tuple(ROLE_TOOLS)

#: Roles belonging to each operating spec — see prompts.system_prompt.
GROWTH_ROLES = tuple(r for r in ROLES if r not in COMMS_ROLE_TOOLS)
COMMS_ROLES = tuple(COMMS_ROLE_TOOLS)


def build_agent(
    role: str,
    workspace: Workspace,
    toolbox: Toolbox | None = None,
    client: ClaudeClient | None = None,
) -> Agent:
    if role not in ROLE_TOOLS:
        raise ValueError(f"unknown role '{role}'; expected one of {', '.join(ROLES)}")
    box = toolbox or Toolbox(workspace)
    allowed = [name for name in ROLE_TOOLS[role] if box.has(name)]
    return Agent(role=role, workspace=workspace, toolbox=box, client=client, allowed_tools=allowed)
