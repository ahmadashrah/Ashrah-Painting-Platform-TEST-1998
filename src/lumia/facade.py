"""One entry point to the whole platform.

Using Lumia from Python currently means knowing that a `Workspace` wraps a
store and integrations, that a `Toolbox` binds tools to it, that growth
work goes through `Orchestrator` and delivery work through
`CommunicationDesk`, and that roles live in a third module. That is a fine
internal structure and a poor front door.

`Lumia` is the front door:

    from lumia import Lumia

    lumia = Lumia()
    lumia.status()                      # what is live, what is mocked
    lumia.agents()                      # every agent and what it may do
    lumia.screen("The change order will be $4,200.")
    lumia.gate("send_communication", draft_id="comm_abc")
    lumia.ask("send today's log to Dana")     # needs an API key

Two properties matter more than the convenience.

**Most of it runs without an API key.** Screening, the autonomy gate,
verification, the daily-log renderer, channel choice, seeding and every
report are deterministic Python. Only `ask()` and the named cycles need the
model, and they say so precisely rather than failing deep in a stack.

**Nothing here is a second implementation.** Every method delegates to the
same code the agents use, so what you see from the facade is what the agent
gets. Changing a rule changes both.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agents import COMMS_ROLES, GROWTH_ROLES, ROLE_TOOLS, build_agent
from .autonomy import TOOL_LEVELS, AutonomyLevel, classify
from .comms.desk import ROUTING_HINTS as COMMS_HINTS
from .comms.desk import CommunicationDesk
from .comms.reporting import communication_review
from .comms.screening import screen as screen_text
from .comms.seed import seed_demo_projects
from .config import SETTINGS, Settings
from .domain.projects import RecipientRole
from .llm import MissingAPIKey, build_client
from .orchestrator import ROUTING_HINTS as GROWTH_HINTS
from .orchestrator import Orchestrator
from .reporting import growth_review
from .runner import Runner, RunRecord
from .seed import seed_demo_data
from .tools import Toolbox
from .workspace import Workspace

#: Order of preference when two specialists match a request equally well.
#: Cautious roles come first: the cost of over-escalating is a manager's
#: minute, and the cost of under-escalating is a buried problem.
TIE_BREAK = ("escalation", "vendor_comms", "intake", "crew_comms", "client_comms")

#: One-line description of what each agent is for, shown by `agents()`.
PURPOSE: dict[str, str] = {
    "director": "Owns the growth operating loop and decides where effort goes.",
    "research": "Finds and qualifies opportunity; turns market signals into actions.",
    "outreach": "Writes and sends account-specific outreach and follow-ups.",
    "content": "Builds proof: case studies, capability statements, project write-ups.",
    "crm": "Keeps the pipeline record accurate and every account managed.",
    "intake": "Turns field submissions — any language, voice, photo — into verified facts.",
    "client_comms": "Writes to clients, GCs, PMs and superintendents; owns the daily log.",
    "crew_comms": "Dispatches crews and chases the missing detail.",
    "vendor_comms": "Handles materials, deliveries and supplier coordination.",
    "escalation": "Prepares what management needs to decide quickly.",
}


@dataclass
class AgentInfo:
    """What one agent is and what it is allowed to do."""

    role: str
    family: str                      # growth | communication
    purpose: str
    tools: list[str]
    autonomous: list[str]            # Level 1 — runs freely
    controlled: list[str]            # Level 2 — runs within approved rules
    approval_required: list[str]     # Level 3 — always queued for a human

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "family": self.family,
            "purpose": self.purpose,
            "tool_count": len(self.tools),
            "tools": self.tools,
            "levels": {
                "1_autonomous": self.autonomous,
                "2_controlled": self.controlled,
                "3_approval_required": self.approval_required,
            },
        }


class Lumia:
    """The platform, as one object."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        settings: Settings | None = None,
        client: ClaudeClient | None = None,
    ) -> None:
        self.settings = settings or SETTINGS
        self.workspace = Workspace.build(
            settings=self.settings, data_dir=Path(data_dir) if data_dir else None
        )
        self.toolbox = Toolbox(self.workspace)
        self._client = client
        self._growth: Orchestrator | None = None
        self._comms: CommunicationDesk | None = None

    # --- the two desks ----------------------------------------------------

    @property
    def growth(self) -> Orchestrator:
        """Marketing: the daily cycle, the weekly review, growth specialists."""
        if self._growth is None:
            self._growth = Orchestrator(
                workspace=self.workspace,
                toolbox=self.toolbox,
                client=self._client or build_client(self.settings),
            )
        return self._growth

    @property
    def comms(self) -> CommunicationDesk:
        """Delivery: intake, daily logs, dispatch, follow-ups, review."""
        if self._comms is None:
            self._comms = CommunicationDesk(
                workspace=self.workspace,
                toolbox=self.toolbox,
                client=self._client or build_client(self.settings),
            )
        return self._comms

    # --- introspection: works with no credentials at all -------------------

    @property
    def live(self) -> bool:
        """Whether the model can actually be called."""
        return bool(self.settings.anthropic_api_key)

    def status(self) -> dict[str, Any]:
        """Model, integrations, data and what is currently blocked."""
        state = dict(self.workspace.status())
        state["can_run_agents"] = self.live
        state["agents"] = len(ROLE_TOOLS)
        state["tools"] = len(self.toolbox.names())
        state["keys"] = self.keys()
        if not self.live:
            state["blocked"] = (
                "ANTHROPIC_API_KEY is not set, so the agents cannot reason or act on their "
                "own. Everything deterministic — screening, the autonomy gate, verification, "
                "daily-log rendering, reports and seeding — still runs."
            )
        return state

    def keys(self) -> dict[str, Any]:
        """Which credentials are set, and the exact variable for each gap.

        "email: mock" tells you something is missing; it does not tell you
        what to type. This does.
        """
        report: dict[str, Any] = {
            "ANTHROPIC_API_KEY": {
                "set": self.live,
                "unlocks": "the agents themselves — without it nothing can reason or act",
                "required": True,
            }
        }
        unlocks = {
            "crm": "mirroring the record to a hosted CRM (the local store works without it)",
            "email": "actually delivering email — daily logs, client updates, supplier orders",
            "sms": "actually delivering texts — arrival notices, access, crew dispatch",
            "calendar": "putting meetings and site walks on a real calendar",
            "search": "real market research instead of simulated results",
            "construction_data": "real permits, tenders and project awards",
            "weather": "real forecasts for exterior scheduling",
        }
        for name, credentials in sorted(self.settings.services.items()):
            missing = credentials.missing_vars
            report[credentials.key_var or name] = {
                "set": not missing,
                "unlocks": unlocks.get(name, name),
                "still_needs": missing,
                "required": False,
            }
        if not self.settings.company_email:
            report["COMPANY_EMAIL"] = {
                "set": False,
                "unlocks": "sending at all — outbound email is refused from an unknown address",
                "required": True,
            }
        return report

    def agents(self, family: str = "") -> list[AgentInfo]:
        """Every agent, its purpose, its tools and what each tool costs it."""
        infos = []
        for role, tools in ROLE_TOOLS.items():
            kind = "communication" if role in COMMS_ROLES else "growth"
            if family and family != kind:
                continue
            available = [t for t in tools if self.toolbox.has(t)]
            by_level: dict[AutonomyLevel, list[str]] = {
                AutonomyLevel.AUTONOMOUS: [],
                AutonomyLevel.CONTROLLED: [],
                AutonomyLevel.APPROVAL_REQUIRED: [],
            }
            for tool in available:
                by_level[TOOL_LEVELS.get(tool, AutonomyLevel.APPROVAL_REQUIRED)].append(tool)
            infos.append(
                AgentInfo(
                    role=role,
                    family=kind,
                    purpose=PURPOSE.get(role, ""),
                    tools=available,
                    autonomous=sorted(by_level[AutonomyLevel.AUTONOMOUS]),
                    controlled=sorted(by_level[AutonomyLevel.CONTROLLED]),
                    approval_required=sorted(by_level[AutonomyLevel.APPROVAL_REQUIRED]),
                )
            )
        return sorted(infos, key=lambda a: (a.family, a.role))

    def agent(self, role: str) -> Any:
        """One configured specialist, ready to run."""
        return build_agent(role, self.workspace, self.toolbox, self._client or build_client(self.settings))

    def tools(self, role: str = "") -> list[dict[str, Any]]:
        """Tool names, levels and descriptions — the whole surface, or one role's."""
        names = [t for t in ROLE_TOOLS[role] if self.toolbox.has(t)] if role else self.toolbox.names()
        schemas = {s["name"]: s for s in self.toolbox.schemas(names)}
        return [
            {
                "name": name,
                "level": int(TOOL_LEVELS.get(name, AutonomyLevel.APPROVAL_REQUIRED)),
                "description": schemas[name]["description"],
            }
            for name in sorted(schemas)
        ]

    # --- deterministic operations: no API key required ----------------------

    def screen(self, text: str, recipient_role: str = RecipientRole.CLIENT.value, subject: str = "") -> dict[str, Any]:
        """Would this message need a human? Same screen the send gate uses."""
        return screen_text(text, recipient_role=recipient_role, subject=subject).to_dict()

    def gate(self, tool: str, **arguments: Any) -> dict[str, Any]:
        """Dry-run the autonomy gate: what level is this call, and why.

        Reads the same account and draft records the live gate reads, so the
        verdict here is the verdict the agent would get.
        """
        account = None
        if arguments.get("account_id"):
            account = self.workspace.crm.get_account(str(arguments["account_id"]))
        draft = None
        if arguments.get("draft_id"):
            draft = self.workspace.comms.get_communication(str(arguments["draft_id"]))

        level, reason = classify(tool, dict(arguments), account, draft)
        return {
            "tool": tool,
            "level": int(level),
            "verdict": {
                1: "runs freely",
                2: "runs within approved rules",
                3: "queued for a human — this would NOT execute",
            }[int(level)],
            "reason": reason,
            "known_tool": tool in TOOL_LEVELS,
        }

    def call(self, tool: str, **arguments: Any) -> Any:
        """Run one tool directly, through the gate.

        A Level 3 call is refused here rather than executed, so driving the
        platform by hand cannot do what the agent is not allowed to do.
        """
        decision = self.gate(tool, **arguments)
        if decision["level"] == 3:
            return {
                "error": "this action requires human approval and was not performed",
                "gate": decision,
            }
        return self.toolbox.call(tool, dict(arguments))

    def route(self, task: str) -> str:
        """Which specialist would take this request, across both families.

        Ties are broken by `TIE_BREAK` rather than by dict order. "The client
        is threatening to escalate" matches client reporting and escalation
        equally, and the two mistakes are not equally cheap: routing an
        ordinary update to escalation wastes a manager's minute, while
        routing a threat to routine reporting buries it.
        """
        text = task.lower()
        scores = {
            role: sum(1 for hint in hints if hint in text)
            for table in (GROWTH_HINTS, COMMS_HINTS)
            for role, hints in table.items()
        }
        best = max(scores, key=lambda role: (scores[role], -TIE_BREAK.index(role) if role in TIE_BREAK else -len(TIE_BREAK)))
        return best if scores[best] > 0 else "director"

    # --- data ---------------------------------------------------------------

    def projects(self) -> list[dict[str, Any]]:
        return self.workspace.comms.list_projects()

    def accounts(self) -> list[dict[str, Any]]:
        return self.workspace.crm.list_accounts()

    def approvals(self) -> list[dict[str, Any]]:
        return self.workspace.approvals.pending()

    def approve(self, request_id: str, note: str = "", execute: bool = True) -> dict[str, Any]:
        """Approve a queued action and, by default, perform it."""
        record = self.workspace.approvals.decide(request_id, approved=True, note=note)
        if "error" in record or not execute:
            return record
        result = self.toolbox.call(record["tool"], {**record["arguments"], "approval_id": record["id"]})
        self.workspace.approvals.mark_executed(
            record["id"], result if isinstance(result, dict) else {"result": result}
        )
        return {"approved": record, "result": result}

    def reject(self, request_id: str, note: str = "") -> dict[str, Any]:
        return self.workspace.approvals.decide(request_id, approved=False, note=note)

    def seed(self) -> dict[str, Any]:
        """Load demo accounts and a demo project so there is something to work on."""
        return {"growth": seed_demo_data(self.workspace), "projects": seed_demo_projects(self.workspace)}

    def reports(self, days: int = 7) -> dict[str, Any]:
        """Both scoreboards, computed — never narrated."""
        return {
            "growth": growth_review(self.workspace, days=days),
            "communication": communication_review(self.workspace, days=days),
        }

    # --- model-backed ---------------------------------------------------------

    def ask(self, task: str, role: str | None = None) -> Any:
        """Give the platform a task and let it pick the right specialist.

        Every call is an isolated run: its own workspace, toolbox, client
        and conversation, built fresh and thrown away. Nothing carries over
        from the previous run — see `runner.py`.
        """
        chosen = role or self.route(task)
        if chosen not in ROLE_TOOLS:
            raise ValueError(f"unknown role '{chosen}'; expected one of {', '.join(sorted(ROLE_TOOLS))}")
        self._require_model(f"ask({task[:40]!r})")
        return self.runner.run(chosen, task)

    @property
    def runner(self) -> Runner:
        """Builds a cold stack per run. Deliberately not cached across runs."""
        return Runner(settings=self.settings, data_dir=self.workspace.settings.data_dir)

    def runs(self, limit: int = 20, role: str = "") -> list[dict[str, Any]]:
        """What each agent has done, newest first."""
        return self.runner.history(limit=limit, role=role)

    def _require_model(self, what: str) -> None:
        if not self.live:
            raise MissingAPIKey(
                f"{what} needs the model, and ANTHROPIC_API_KEY is not set. "
                "Add it to .env or export it. Everything deterministic — screen(), gate(), "
                "call(), seed(), reports(), agents() — works without it."
            )

    def __repr__(self) -> str:
        state = "live" if self.live else "no API key"
        return f"<Lumia {len(ROLE_TOOLS)} agents, {len(self.toolbox.names())} tools, {state}>"
