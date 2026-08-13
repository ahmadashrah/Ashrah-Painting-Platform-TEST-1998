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
import signal
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

try:  # POSIX file locking, so concurrent processes cannot take the same number
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from .agent import MAX_ITERATIONS, AgentRun
from .agents import ROLE_TOOLS, build_agent
from .agents import COMMS_ROLES
from .config import SETTINGS, Settings
from .deadline import Deadline, budget_for
from .domain.projects import new_id
from .llm import build_client
from .observability import (
    HOOKS, PHASE_FINISHED, PHASE_STARTED, RUN_FAILED, RUN_FINISHED, RUN_KILLED, RUN_STARTED,
    JsonlRecorder, KillSwitch, RunObserver,
)
from .phases import Phase, plan_for
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


#: How long past its budget a run is allowed to keep going before it is
#: interrupted outright. The gap exists so the cooperative stop — which
#: exits cleanly and reports what happened — normally wins the race.
WATCHDOG_GRACE_SECONDS = 15.0


class HardTimeout(RuntimeError):
    """A run was interrupted because it stopped policing itself."""


@contextmanager
def _watchdog(seconds: float, reference: str) -> Iterator[None]:
    """Interrupt a run that has stopped checking its own budget.

    The deadline in `deadline.py` is cooperative: it works because the loop
    looks at it between turns and before tool calls. That covers the runs
    that are merely slow. It does not cover a run blocked *below* that
    level — a socket opened without a timeout, a library that swallows the
    one we passed, a retry loop inside a vendor SDK. Nothing is checking
    anything there, so nothing stops.

    A timer signal interrupts the blocked call itself, which is the only
    thing that reliably does.

    This needs the main thread of a POSIX process. Under a thread pool or
    on Windows it does nothing, and the cooperative checks are all there
    is — which is precisely why those are placed at three points rather
    than one, rather than being left to a watchdog that may not be there.
    """
    available = (
        hasattr(signal, "SIGALRM")
        and threading.current_thread() is threading.main_thread()
    )
    if not available or seconds <= 0:
        yield
        return

    def interrupt(_signum: int, _frame: Any) -> None:
        raise HardTimeout(
            f"{reference} was interrupted {seconds:.0f}s in: it stopped responding to its "
            "own time budget. Anything already sent stands; nothing further was attempted."
        )

    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


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
    #: None when the run was not time-capped.
    budget_seconds: float | None = None
    timed_out: bool = False
    killed: bool = False
    #: One entry per phase: what it was for, what it used, how it ended.
    phases: list[dict[str, Any]] = field(default_factory=list)
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
        self._recorder_attached = False

    # --- the operator's window ---------------------------------------------

    @property
    def root(self) -> Path:
        return Path(self.data_dir or self.settings.data_dir)

    @property
    def kill_switch(self) -> KillSwitch:
        return KillSwitch(self.root / "kill")

    def recorder(self) -> JsonlRecorder:
        """The step log every run writes to, attached once per Runner."""
        recorder = JsonlRecorder(self.root / "events.jsonl")
        if not self._recorder_attached:
            HOOKS.subscribe(recorder, name="jsonl")
            self._recorder_attached = True
        return recorder

    def steps(self, reference: str, limit: int = 500) -> list[dict[str, Any]]:
        """Every step of one run, in order."""
        return JsonlRecorder(self.root / "events.jsonl").read(run=reference, limit=limit)

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

    def run(
        self,
        role: str,
        task: str,
        max_iterations: int = MAX_ITERATIONS,
        budget_seconds: float | None = None,
    ) -> RunRecord:
        """Run one agent on one task, from a cold start, under its own number."""
        if role not in ROLE_TOOLS:
            raise ValueError(f"unknown role '{role}'; expected one of {', '.join(sorted(ROLE_TOOLS))}")

        workspace, toolbox = self.build()

        # The number is issued before anything else happens, so a run that
        # fails on its first turn is still findable by reference.
        number = self.counter(workspace).next()
        # Communication is time-critical: a crew at a locked door or a client
        # waiting on the log is not served by a perfect message that is late.
        budget = budget_seconds if budget_seconds is not None else budget_for(role, COMMS_ROLES)
        deadline = Deadline(
            budget_seconds=budget,
            label="communication" if role in COMMS_ROLES else "growth work",
        )
        record = RunRecord(
            role=role, task=task, number=number, reference=reference_for(number),
            budget_seconds=budget,
        )

        # Everything this run writes is stamped with the reference — see
        # LocalStore.put. The workspace is the run's scope, so the run's
        # identity lives on it rather than being threaded through every call.
        workspace.stamp_run(record.reference)

        recorder = self.recorder()
        observer = RunObserver(run=record.reference, role=role)
        switch = self.kill_switch
        # Not cleared here: a reference is never reused, so a request for this
        # run can only ever have been meant for this run — including one armed
        # before it started. It is consumed when the run ends instead.

        agent = build_agent(role, workspace, toolbox, self.client_factory(self.settings))
        workspace.store.put("runs", record.id, record.to_dict())
        started = datetime.now()
        observer.emit(RUN_STARTED, task=task, budget_seconds=budget, number=number)

        try:
            watchdog_after = (
                deadline.budget_seconds + WATCHDOG_GRACE_SECONDS if deadline.limited else 0.0
            )
            with _watchdog(watchdog_after, record.reference):
                result = self._run_phases(
                    agent=agent,
                    record=record,
                    task=task,
                    deadline=deadline,
                    observer=observer,
                    switch=switch,
                )
        except HardTimeout as exc:
            record.status = "failed"
            record.timed_out = True
            record.error = str(exc)
            log.warning("run %s hard-stopped: %s", record.reference, exc)
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
            record.timed_out = result.timed_out
            record.killed = getattr(result, "killed", False)

        record.finished_at = _timestamp()
        record.duration_seconds = round((datetime.now() - started).total_seconds(), 2)
        workspace.store.put("runs", record.id, record.to_dict())
        switch.clear(record.reference)
        observer.emit(
            RUN_FAILED if record.status == "failed" else RUN_FINISHED,
            status=record.status,
            seconds=record.duration_seconds,
            actions=len(record.tools_executed),
            held=len(record.tools_gated),
            timed_out=record.timed_out,
            killed=record.killed,
            error=record.error,
        )
        return record

    # --- phases -----------------------------------------------------------

    def _run_phases(
        self,
        agent: Any,
        record: RunRecord,
        task: str,
        deadline: Deadline,
        observer: RunObserver,
        switch: KillSwitch,
    ) -> AgentRun:
        """Work the role's phases in order, inside one run.

        Each phase is its own conversation holding only its own tools. What
        carries forward is the previous phase's written result, not its
        transcript — a phase hands the next its findings, which is what a
        handoff is, and it keeps the context that reaches the model bounded.
        """
        phases = plan_for(agent.role, agent.allowed_tools)
        combined = AgentRun(role=agent.role, task=task)
        findings: list[str] = []
        role_tools = list(agent.allowed_tools or [])

        for index, phase in enumerate(phases, start=1):
            if not phase.tools:
                continue                      # this role holds none of them

            # The phase loop stops for the same reasons the turn loop does, and
            # must set the same flags — a run stopped between phases is just as
            # killed as one stopped between turns, and reporting it as a clean
            # finish would be the worst kind of quiet failure.
            stop = switch.requested(record.reference)
            if stop is not None:
                combined.killed = True
                combined.stopped_because = "killed"
                combined.reply = (
                    f"Stopped by an operator before the {phase.name} phase — "
                    f"{stop.get('reason') or 'no reason given'}. "
                    + (f"Completed phases: {', '.join(p['phase'] for p in record.phases)}. "
                       if record.phases else "Nothing was done. ")
                    + "Anything not listed did not happen."
                )
                observer.emit(RUN_KILLED, reason=str(stop.get("reason")), before=f"the {phase.name} phase")
                break

            if not deadline.usable:
                combined.timed_out = True
                combined.stopped_because = "deadline_exceeded"
                combined.reply = (
                    f"Stopped at the {float(deadline.budget_seconds):.0f}-second limit before the "
                    f"{phase.name} phase. "
                    + (f"Completed phases: {', '.join(p['phase'] for p in record.phases)}. "
                       if record.phases else "Nothing was done. ")
                    + "Anything not listed did not happen."
                )
                break

            observer.emit(
                PHASE_STARTED, phase=phase.name, index=index, of=len(phases),
                goal=phase.goal, tools=", ".join(phase.tools),
                remaining_seconds=deadline.remaining,
            )

            agent.allowed_tools = phase.tools
            agent.phase = phase.name
            outcome = agent.run(
                _phase_prompt(task, phase, index, len(phases), findings),
                max_iterations=phase.max_iterations,
                deadline=deadline,
                observer=observer,
                kill_switch=switch,
            )

            combined.tool_calls.extend(outcome.tool_calls)
            combined.approvals_raised.extend(outcome.approvals_raised)
            combined.iterations += outcome.iterations
            combined.reply = outcome.reply or combined.reply
            combined.timed_out = combined.timed_out or outcome.timed_out
            combined.killed = combined.killed or outcome.killed
            combined.stopped_because = outcome.stopped_because

            used = [c.tool for c in outcome.tool_calls if c.executed]
            refused = [c.tool for c in outcome.tool_calls if "outside this phase" in c.result_summary]
            record.phases.append({
                "phase": phase.name,
                "goal": phase.goal,
                "index": index,
                "available_tools": phase.tools,
                "tools_used": used,
                "tools_refused": refused,
                "turns": outcome.iterations,
                "stopped_because": outcome.stopped_because,
                "result": outcome.reply,
            })
            observer.emit(
                PHASE_FINISHED, phase=phase.name, index=index,
                turns=outcome.iterations, used=", ".join(used) or "none",
                refused=", ".join(refused), stopped=outcome.stopped_because,
            )

            if outcome.reply.strip():
                findings.append(f"{phase.name}: {outcome.reply.strip()}")

            if outcome.killed or outcome.timed_out:
                break

        agent.allowed_tools = role_tools
        agent.phase = ""
        return combined

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


def _phase_prompt(task: str, phase: Phase, index: int, total: int, findings: list[str]) -> str:
    """What one phase is asked to do, and what the last one found.

    The tool list is stated rather than left implicit: an agent that knows
    what it may reach for asks for the right thing, instead of proposing a
    send from a gathering phase and being refused.
    """
    handoff = (
        "\n\nWhat earlier phases established — treat as given, do not redo:\n"
        + "\n".join(f"- {f}" for f in findings)
        if findings else ""
    )
    return f"""\
{task}

--- Phase {index} of {total}: {phase.name} ---
{phase.goal}

In this phase you may use only: {', '.join(phase.tools) or 'no tools'}.
Anything else is unavailable here and will be refused — later phases hold
the rest. Do this phase's work, then stop and state plainly what you found
or did, so the next phase can build on it.{handoff}
"""
