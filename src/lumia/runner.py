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
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:  # POSIX file locking, so concurrent processes cannot take the same number
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from .agent import MAX_ITERATIONS, AgentRun
from .agents import ROLE_TOOLS, build_agent
from .config import SETTINGS, Settings
from .domain.projects import new_id
from .llm import build_client
from .tools import Toolbox
from .workspace import Workspace

log = logging.getLogger(__name__)


#: How a run number is written wherever a person will read it.
REFERENCE_PREFIX = "RUN"
REFERENCE_WIDTH = 6


def reference_for(number: int) -> str:
    return f"{REFERENCE_PREFIX}-{number:0{REFERENCE_WIDTH}d}"


class RunCounter:
    """Hands out run numbers, one at a time, to everyone.

    A number has to be unique across every agent and every process, and it
    has to be issued *before* the run does anything — a run that fails on
    its first turn still has to be findable by number.

    Isolation makes this harder than a counter usually is: each run builds
    its own store object, so an in-process lock protects nothing against a
    second worker. The count therefore lives in its own small file, and the
    increment is done under an exclusive file lock.

    If the counter file is lost, the next number is taken from the highest
    run already recorded rather than restarting at one. Reusing a number
    would be worse than skipping a range: two different runs answering to
    the same reference makes every record that cites it ambiguous.
    """

    def __init__(self, path: Path, floor: Callable[[], int] | None = None) -> None:
        self.path = path
        self.floor = floor or (lambda: 0)
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a+", encoding="utf-8") as handle:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    handle.seek(0)
                    raw = handle.read().strip()
                    try:
                        current = int(raw)
                    except ValueError:
                        current = 0
                    # Never hand back a number the record has already used.
                    current = max(current, self.floor())
                    nxt = current + 1
                    handle.seek(0)
                    handle.truncate()
                    handle.write(str(nxt))
                    handle.flush()
                finally:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return nxt


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
    #: Issued before the run starts, unique across every agent, and stamped
    #: on everything the run writes. `number` orders runs; `reference` is
    #: what a person quotes.
    number: int = 0
    reference: str = ""
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

    @property
    def tool_calls(self) -> list[Any]:
        """Executed and gated calls, in the shape the CLI reporter reads."""
        from types import SimpleNamespace

        return [
            SimpleNamespace(tool=t, executed=True, level=0, result_summary="done")
            for t in self.tools_executed
        ] + [
            SimpleNamespace(tool=t, executed=False, level=3, result_summary="queued for approval")
            for t in self.tools_gated
        ]


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
        self._counter: RunCounter | None = None

    # --- numbering ---------------------------------------------------------

    def counter(self, workspace: Workspace) -> RunCounter:
        if self._counter is None:
            root = Path(self.data_dir or workspace.settings.data_dir)
            self._counter = RunCounter(root / "run-counter", floor=lambda: _highest_run(workspace))
        return self._counter

    # --- building one cold stack -----------------------------------------

    def build(self) -> tuple[Workspace, Toolbox]:
        """A workspace and toolbox belonging to exactly one run."""
        workspace = Workspace.build(settings=self.settings, data_dir=self.data_dir)
        return workspace, Toolbox(workspace)

    def run(self, role: str, task: str, max_iterations: int = MAX_ITERATIONS) -> RunRecord:
        """Run one agent on one task, from a cold start, under its own number."""
        if role not in ROLE_TOOLS:
            raise ValueError(f"unknown role '{role}'; expected one of {', '.join(sorted(ROLE_TOOLS))}")

        workspace, toolbox = self.build()

        # The number is issued before anything else happens, so a run that
        # fails on its first turn is still findable by reference.
        number = self.counter(workspace).next()
        record = RunRecord(role=role, task=task, number=number, reference=reference_for(number))

        # Everything this run writes is stamped with the reference — see
        # LocalStore.put. The workspace is the run's scope, so the run's
        # identity lives on it rather than being threaded through every call.
        workspace.stamp_run(record.reference)

        agent = build_agent(role, workspace, toolbox, self.client_factory(self.settings))
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
        # Numbered runs order by number; the timestamp is the tie-break for
        # any record written before numbering existed.
        return sorted(
            records,
            key=lambda r: (int(r.get("number", 0) or 0), str(r.get("started_at", ""))),
            reverse=True,
        )[:limit]

    def get(self, reference: str) -> dict[str, Any] | None:
        """One run by its number or reference — 'RUN-000042', 'run-42' or '42'."""
        workspace, _ = self.build()
        wanted = str(reference).strip().upper().removeprefix(f"{REFERENCE_PREFIX}-").lstrip("0")
        for record in workspace.store.list("runs"):
            if record.get("reference") == reference or str(record.get("number", "")) == (wanted or "0"):
                return record
        return None

    def trace(self, reference: str) -> dict[str, Any]:
        """Everything one run touched, by reference.

        This is what the number is *for*: a record stamped with a run can be
        traced back to the job that produced it, and back out to everything
        else that job did.
        """
        workspace, _ = self.build()
        record = self.get(reference)
        if record is None:
            return {"error": f"no run matching {reference!r}"}

        ref = str(record.get("reference", ""))
        touched: dict[str, list[dict[str, Any]]] = {}
        for collection in ("communications", "escalations", "open_items", "submissions",
                           "media", "daily_logs", "accounts", "interactions", "lessons"):
            hits = [r for r in workspace.store.list(collection) if r.get("_run") == ref]
            if hits:
                touched[collection] = [
                    {k: v for k, v in r.items() if k in ("id", "subject", "status", "summary", "question", "name")}
                    for r in hits
                ]
        return {
            "run": record,
            "approvals": [a for a in workspace.approvals.all() if a.get("run_ref") == ref],
            "touched": touched,
        }


def _highest_run(workspace: Workspace) -> int:
    numbers = [int(r.get("number", 0) or 0) for r in workspace.store.list("runs")]
    return max(numbers, default=0)
