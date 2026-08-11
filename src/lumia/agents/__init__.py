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

ROLES = tuple(ROLE_TOOLS)


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
