"""The operator's window: every step, as it happens, and a way to stop it.

Two capabilities that belong together.

**Every step is an event.** The loop announces what it is doing at each
point a person would want to see: the run starting, each turn, what the
model asked for, what the gate decided, what actually ran and what came
back, and how the run ended. Events go to any number of subscribers — a
JSONL file by default, plus whatever an operator registers.

**A run can be stopped from outside.** The time budget in `deadline.py` is
the run policing itself; this is somebody else policing it. An operator
who sees a run doing the wrong thing can kill it by reference, and the loop
stops at its next step without leaving half-finished work.

Three rules hold this together.

*A hook must never break a run.* Subscriber exceptions are caught and
logged. An operator's broken dashboard cannot take down the crew dispatch.

*A hook must never slow a run past its budget.* Emission is fire-and-forget
against the same clock everything else uses; a slow subscriber loses its
events rather than the run losing its deadline.

*Killing stops new work, it does not interrupt committed work.* Same rule
as the deadline, for the same reason: a send cancelled mid-write may still
have been delivered, and a message the record shows as unsent but the
client received is the worse outcome.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Event kinds, in the order a healthy run produces them.
RUN_STARTED = "run.started"
TURN_STARTED = "turn.started"
MODEL_REPLIED = "model.replied"
TOOL_PROPOSED = "tool.proposed"
GATE_DECIDED = "gate.decided"
TOOL_EXECUTED = "tool.executed"
TOOL_GATED = "tool.gated"
TOOL_REFUSED = "tool.refused"
PHASE_STARTED = "phase.started"
PHASE_FINISHED = "phase.finished"
BUDGET_WARNING = "budget.warning"
RUN_FINISHED = "run.finished"
RUN_FAILED = "run.failed"
RUN_KILLED = "run.killed"
CONTRACT_BREACHED = "contract.breached"

#: Warn the operator once a run has spent this much of its budget, so a
#: slow run is visible before it is a late one.
WARN_AT_FRACTION = 0.75

#: Events are truncated to this before being written — a tool result can be
#: 60KB, and the step log is for watching, not for storing payloads.
MAX_DETAIL_CHARS = 1_200


@dataclass
class Event:
    """One step in a run."""

    run: str
    kind: str
    role: str = ""
    seq: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="milliseconds"))
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def line(self) -> str:
        """One human-readable line, for a terminal or a log."""
        bits = " ".join(f"{k}={v}" for k, v in self.detail.items() if v not in (None, "", [], {}))
        return f"{self.at[11:23]}  {self.run}  {self.kind:<16} {bits}"[:400]


Subscriber = Callable[[Event], None]


class HookRegistry:
    """Where operator hooks attach.

    Deliberately global rather than passed through: an operator wants to
    see *everything*, including runs started by code they did not write and
    agents that do not exist yet.
    """

    def __init__(self) -> None:
        self._subscribers: list[tuple[str, Subscriber]] = []
        self._lock = threading.Lock()

    def subscribe(self, subscriber: Subscriber, name: str = "") -> Callable[[], None]:
        """Register a hook. Returns a function that removes it again."""
        entry = (name or getattr(subscriber, "__name__", "hook"), subscriber)
        with self._lock:
            self._subscribers.append(entry)

        def unsubscribe() -> None:
            with self._lock:
                if entry in self._subscribers:
                    self._subscribers.remove(entry)

        return unsubscribe

    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()

    @property
    def names(self) -> list[str]:
        with self._lock:
            return [name for name, _ in self._subscribers]

    def emit(self, event: Event) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for name, subscriber in subscribers:
            try:
                subscriber(event)
            except Exception as exc:
                # An operator's broken dashboard must not take down a crew
                # dispatch. Log it and carry on.
                log.warning("observer %r failed on %s: %s", name, event.kind, exc)


#: The registry every run emits into.
HOOKS = HookRegistry()


class RunObserver:
    """Emits the steps of one run, numbering them as it goes."""

    def __init__(self, run: str, role: str, registry: HookRegistry | None = None) -> None:
        self.run = run
        self.role = role
        self.registry = registry or HOOKS
        self.started = time.monotonic()
        self._seq = 0
        self._lock = threading.Lock()
        self._warned = False

    def emit(self, kind: str, **detail: Any) -> Event:
        with self._lock:
            self._seq += 1
            seq = self._seq
        event = Event(
            run=self.run,
            kind=kind,
            role=self.role,
            seq=seq,
            detail=_trim(detail),
            elapsed_seconds=round(time.monotonic() - self.started, 2),
        )
        self.registry.emit(event)
        return event

    def budget_check(self, deadline: Any) -> None:
        """Warn once when a run is running late, before it is too late."""
        if self._warned or deadline is None:
            return
        spent = deadline.elapsed / deadline.budget_seconds if deadline.budget_seconds else 0
        if spent >= WARN_AT_FRACTION:
            self._warned = True
            self.emit(
                BUDGET_WARNING,
                spent_seconds=deadline.elapsed,
                budget_seconds=deadline.budget_seconds,
                remaining_seconds=deadline.remaining,
            )


def _trim(detail: dict[str, Any]) -> dict[str, Any]:
    trimmed: dict[str, Any] = {}
    for key, value in detail.items():
        text = value if isinstance(value, (int, float, bool, type(None))) else str(value)
        if isinstance(text, str) and len(text) > MAX_DETAIL_CHARS:
            text = text[:MAX_DETAIL_CHARS] + f"… (+{len(text) - MAX_DETAIL_CHARS} chars)"
        trimmed[key] = text
    return trimmed


# --- built-in subscribers ----------------------------------------------------


class JsonlRecorder:
    """Appends every event to a file, one JSON object per line.

    Append-only and line-delimited on purpose: an operator can `tail -f` it
    while a run is in progress, and a crash mid-write costs one line rather
    than the file.
    """

    def __init__(self, path: Path, max_bytes: int = 8 * 1024 * 1024) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def __call__(self, event: Event) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Roll once rather than growing without bound. One generation of
            # history is enough to investigate what just happened.
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                self.path.replace(self.path.with_suffix(".1"))
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), default=str) + "\n")

    def read(self, run: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        events = []
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue          # a torn final line is not a reason to fail
                if not run or record.get("run") == run:
                    events.append(record)
        return events[-limit:]


def console(event: Event) -> None:
    """Print each step. The simplest possible operator window."""
    print(event.line(), flush=True)


# --- the kill switch ----------------------------------------------------------


class KillSwitch:
    """Lets an operator stop a run that is already going.

    File-based rather than in-memory, because the run being stopped is
    usually in another process — a scheduled cycle, a web request, a
    worker. A file both processes can see is the whole mechanism.

    `ALL` stops everything currently running, for the case where an
    operator needs the system to stop now and will work out which run was
    at fault afterwards.
    """

    ALL = "ALL"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, reference: str) -> Path:
        safe = "".join(c for c in reference if c.isalnum() or c in "-_")
        return self.directory / safe

    def request(self, reference: str, reason: str = "") -> dict[str, Any]:
        """Ask a run to stop at its next step."""
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(reference)
        path.write_text(
            json.dumps({"reason": reason, "at": datetime.now().isoformat(timespec="seconds")}),
            encoding="utf-8",
        )
        return {"killing": reference, "reason": reason, "note": "the run stops at its next step"}

    def requested(self, reference: str) -> dict[str, Any] | None:
        """Whether this run — or everything — has been asked to stop."""
        for candidate in (reference, self.ALL):
            path = self._path(candidate)
            if path.is_file():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    payload = {}
                return {"scope": candidate, **payload}
        return None

    def clear(self, reference: str) -> None:
        """Consume the request, so the next run is not killed by a stale file."""
        for candidate in (reference, self.ALL):
            path = self._path(candidate)
            if path.is_file() and candidate == reference:
                path.unlink(missing_ok=True)

    def pending(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(p.name for p in self.directory.iterdir() if p.is_file())

    def release_all(self) -> None:
        for path in self.directory.glob("*") if self.directory.is_dir() else []:
            path.unlink(missing_ok=True)


class Killed(RuntimeError):
    """Raised inside a run that an operator has stopped."""
