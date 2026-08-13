"""Isolated runs: every job starts cold.

Two guarantees, and they are the whole point of this module.

**Agents do not share anything mutable.** A run builds its own `Workspace`,
its own `Toolbox`, its own API client and its own agent. Two agents running
against the same data directory touch the same *file*, but never the same
Python object, so nothing one agent does can reach into another's state.

**A run never inherits the last one.** The conversation starts from the
task and nothing else — no prior messages, no accumulated tool results, no
reused agent instance. Run the same agent twice with the same task and it
does the same work twice, in ignorance of the first attempt.

What deliberately *does* persist is the record: projects, field
submissions, sent messages, approvals and escalations. Those are the audit
trail, and a system that forgot them every run could not tell a client what
it said yesterday. The distinction is:

    run state   — conversation, objects, context   → discarded every run
    the record  — what happened, what was sent     → kept, that is the point

`RunRecord` is written for every run, so "which agent did what, when, and
was it isolated" is answerable after the fact rather than assumed.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .agent import MAX_ITERATIONS, AgentRun
from .agents import ROLE_TOOLS, build_agent
from .config import SETTINGS, Settings
from .domain.projects import new_id
from .llm import build_client
from .tools import Toolbox
from .workspace import Workspace

log = logging.getLogger(__name__)


def _timestamp() -> str:
    """Microsecond precision, unlike the record timestamps elsewhere.

    Several runs can start inside the same second, and history has to order
    them correctly — truncating to seconds makes the sort fall back to
    whatever order the store happens to return.
    """
    return datetime.now().isoformat()


@dataclass
class RunRecord:
    """One agent run, start to finish. Written whether it succeeded or not."""

    role: str
    task: str
    status: str = "running"          # running | finished | failed
    reply: str = ""
    stopped_because: str = ""
    iterations: int = 0
    tools_executed: list[str] = field(default_factory=list)
    tools_gated: list[str] = field(default_factory=list)
    approvals_raised: list[str] = field(default_factory=list)
    error: str = ""
    duration_seconds: float = 0.0
    #: Always true. Recorded rather than assumed, so an audit does not have
    #: to take the docstring's word for it.
    isolated: bool = True
    started_at: str = field(default_factory=_timestamp)
    finished_at: str = ""
    id: str = field(default_factory=lambda: new_id("run"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        parts = [f"{self.iterations} turn(s)", f"{len(self.tools_executed)} tool call(s)"]
        if self.tools_gated:
            parts.append(f"{len(self.tools_gated)} awaiting approval")
        return ", ".join(parts)


class Runner:
    """Builds a complete, disposable stack for each run."""

    def __init__(
        self,
        settings: Settings | None = None,
        data_dir: str | Path | None = None,
        client_factory: Callable[[Settings], Any] | None = None,
    ) -> None:
        self.settings = settings or SETTINGS
        self.data_dir = Path(data_dir) if data_dir else None
        # A factory, not a client: reusing one client across runs would be
        # the exact sharing this module exists to prevent. Tests pass a
        # factory that returns a fresh scripted fake each time.
        self.client_factory = client_factory or (lambda s: build_client(s))

    # --- building one cold stack -----------------------------------------

    def build(self) -> tuple[Workspace, Toolbox]:
        """A workspace and toolbox belonging to exactly one run."""
        workspace = Workspace.build(settings=self.settings, data_dir=self.data_dir)
        return workspace, Toolbox(workspace)

    def run(self, role: str, task: str, max_iterations: int = MAX_ITERATIONS) -> RunRecord:
        """Run one agent on one task, from a cold start."""
        if role not in ROLE_TOOLS:
            raise ValueError(f"unknown role '{role}'; expected one of {', '.join(sorted(ROLE_TOOLS))}")

        workspace, toolbox = self.build()
        agent = build_agent(role, workspace, toolbox, self.client_factory(self.settings))

        record = RunRecord(role=role, task=task)
        workspace.store.put("runs", record.id, record.to_dict())
        started = datetime.now()

        try:
            result: AgentRun = agent.run(task, max_iterations=max_iterations)
        except Exception as exc:  # a failed run is still a run, and still recorded
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
            log.warning("run %s (%s) failed: %s", record.id, role, record.error)
        else:
            record.status = "finished"
            record.reply = result.reply
            record.stopped_because = result.stopped_because
            record.iterations = result.iterations
            record.tools_executed = [c.tool for c in result.tool_calls if c.executed]
            record.tools_gated = [c.tool for c in result.tool_calls if not c.executed]
            record.approvals_raised = list(result.approvals_raised)

        record.finished_at = _timestamp()
        record.duration_seconds = round((datetime.now() - started).total_seconds(), 2)
        workspace.store.put("runs", record.id, record.to_dict())
        return record

    # --- history --------------------------------------------------------

    def history(self, limit: int = 20, role: str = "") -> list[dict[str, Any]]:
        """Past runs, newest first. Reads the record, not memory."""
        workspace, _ = self.build()
        records = workspace.store.list("runs")
        if role:
            records = [r for r in records if r.get("role") == role]
        return sorted(records, key=lambda r: str(r.get("started_at", "")), reverse=True)[:limit]
