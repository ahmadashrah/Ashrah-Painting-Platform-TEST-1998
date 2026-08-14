"""L5: many runtimes, one execution fabric.

At L4 each runtime enforces its own contract and a single run is
predictable. That is enough while everything runs in one process. It stops
being enough the moment Ashrah has the daily-log cycle on a scheduler, an
operator running intake from a laptop, and the web console serving a phone
— three runtimes, no shared view, and no way to stop them together.

L5 joins them. Five controls, each answering a question a single runtime
cannot:

1. **Shared runtime registry** — *what is running right now, and where?*
2. **Distributed lifecycle** — *stop everything, from anywhere.*
3. **Contract distribution** — *is every node enforcing the same rules?*
4. **Cross-runtime containment** — *can nodes call each other?*
5. **Coordinated phase transitions** — *has the fleet finished reasoning
   before anything starts sending?*

**What backs this.** The shared data directory, with file locks — the same
substrate the run counter already uses. That coordinates every process on
one host, which is what this deployment is. Nothing here assumes it: the
registry, the bus and the contract store are narrow interfaces over a
`Path`, so swapping in Redis or Postgres for genuine multi-host work means
replacing the storage inside these classes and nothing above them.

Claiming otherwise would be the failure this module exists to prevent — a
fleet that reports consistency it does not have is worse than one that
admits it is a single host.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

#: A run missing from the registry for this long is treated as gone. Its
#: process died without deregistering; holding it forever would block every
#: fleet-wide barrier behind a run that no longer exists.
STALE_AFTER_SECONDS = 90.0

#: How often a long-running run refreshes its registry entry.
HEARTBEAT_SECONDS = 20.0


def node_name() -> str:
    """Where this runtime is. Explicit beats inferred for a fleet."""
    return os.environ.get("LUMIA_NODE", "").strip() or socket.gethostname()


# --- 1. shared runtime registry ---------------------------------------------


@dataclass
class RuntimeEntry:
    """One run, as the fleet sees it."""

    run: str
    role: str
    node: str
    phase: str = "starting"
    status: str = "running"        # running | paused
    task: str = ""
    pid: int = field(default_factory=os.getpid)
    contract_version: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    heartbeat: float = field(default_factory=time.time)

    @property
    def stale(self) -> bool:
        return (time.time() - self.heartbeat) > STALE_AFTER_SECONDS

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["stale"] = self.stale
        return data


class RuntimeRegistry:
    """Every active run across every runtime, with where and how far along.

    One file per run rather than one shared file: two runtimes registering
    at the same moment then touch different paths, so the common case needs
    no lock at all and a crashed process leaves one stale entry rather than
    a corrupted index.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, run: str) -> Path:
        safe = "".join(c for c in run if c.isalnum() or c in "-_")
        return self.directory / f"{safe}.json"

    def register(self, entry: RuntimeEntry) -> RuntimeEntry:
        self.directory.mkdir(parents=True, exist_ok=True)
        _write_json(self._path(entry.run), entry.to_dict())
        log.debug("registered %s on %s", entry.run, entry.node)
        return entry

    def update(self, run: str, **changes: Any) -> dict[str, Any] | None:
        record = self.get(run)
        if record is None:
            return None
        record.update(changes)
        record["heartbeat"] = time.time()
        _write_json(self._path(run), record)
        return record

    def heartbeat(self, run: str) -> None:
        self.update(run)

    def get(self, run: str) -> dict[str, Any] | None:
        return _read_json(self._path(run))

    def deregister(self, run: str) -> None:
        self._path(run).unlink(missing_ok=True)

    def active(self, include_stale: bool = False) -> list[dict[str, Any]]:
        if not self.directory.is_dir():
            return []
        entries = []
        for path in sorted(self.directory.glob("*.json")):
            record = _read_json(path)
            if record is None:
                continue
            record["stale"] = (time.time() - float(record.get("heartbeat", 0))) > STALE_AFTER_SECONDS
            if record["stale"] and not include_stale:
                continue
            entries.append(record)
        return entries

    def sweep(self) -> list[str]:
        """Drop entries whose process died without deregistering."""
        dropped = []
        for record in self.active(include_stale=True):
            if record.get("stale"):
                self.deregister(str(record["run"]))
                dropped.append(str(record["run"]))
        return dropped


# --- 2. distributed lifecycle commands ----------------------------------------

TERMINATE = "terminate"
PAUSE = "pause"
RESUME = "resume"
LIFECYCLE_COMMANDS = (TERMINATE, PAUSE, RESUME)

#: Addressed to every run in the fleet.
ALL = "ALL"


class LifecycleBus:
    """Start, pause, resume and terminate, issued once and seen everywhere.

    The kill switch already stopped a run from outside; this generalizes it
    so an operator can also hold the fleet still — pause every run, look at
    what is happening, then resume — without terminating work that was
    doing nothing wrong.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, target: str) -> Path:
        safe = "".join(c for c in target if c.isalnum() or c in "-_")
        return self.directory / safe

    def broadcast(self, command: str, target: str = ALL, reason: str = "") -> dict[str, Any]:
        if command not in LIFECYCLE_COMMANDS:
            raise ValueError(f"unknown lifecycle command '{command}'")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(target)

        if command == RESUME:
            # Resuming is the removal of a hold, not a fourth state to store.
            existing = _read_json(path) or {}
            if existing.get("command") == PAUSE:
                path.unlink(missing_ok=True)
                return {"command": RESUME, "target": target, "released": True}
            return {"command": RESUME, "target": target, "released": False,
                    "note": "there was no pause to lift"}

        _write_json(path, {
            "command": command,
            "target": target,
            "reason": reason,
            "issued_by": node_name(),
            "at": datetime.now().isoformat(timespec="seconds"),
        })
        return {"command": command, "target": target, "reason": reason,
                "note": f"every runtime sees this at its next step"}

    def pending_for(self, run: str) -> dict[str, Any] | None:
        """The command in force for this run — its own, or the fleet's."""
        for candidate in (run, ALL):
            record = _read_json(self._path(candidate))
            if record:
                return {"scope": candidate, **record}
        return None

    def clear(self, target: str) -> None:
        self._path(target).unlink(missing_ok=True)

    def outstanding(self) -> list[dict[str, Any]]:
        if not self.directory.is_dir():
            return []
        return [r for r in (_read_json(p) for p in sorted(self.directory.iterdir())) if r]


# --- 3. consistent contract distribution ---------------------------------------


@dataclass
class ExecutionContract:
    """The rules every runtime must be enforcing, as one versioned object.

    The version is a fingerprint of the rules themselves rather than a
    number someone remembers to bump. Two nodes agreeing on the version
    therefore means their rules genuinely match — a node running a stale
    deploy produces a different fingerprint and is caught, which is the
    whole point of distributing the contract rather than assuming it.
    """

    contract_id: str
    tool_levels: dict[str, int]
    auto_sendable_kinds: list[str]
    phase_plans: dict[str, list[dict[str, Any]]]
    allowed_hosts: list[str]
    on_breach: str = "kill"
    version: str = ""
    published_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    published_by: str = field(default_factory=node_name)

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "tool_levels": self.tool_levels,
                "auto_sendable_kinds": sorted(self.auto_sendable_kinds),
                "phase_plans": self.phase_plans,
                "allowed_hosts": sorted(self.allowed_hosts),
                "on_breach": self.on_breach,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["version"] = self.version or self.fingerprint()
        return data


class ContractDrift(RuntimeError):
    """This runtime is not enforcing the rules the fleet agreed on."""


class ContractStore:
    """Where the fleet's contract lives, and how a node checks itself."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def publish(self, contract: ExecutionContract) -> dict[str, Any]:
        self.directory.mkdir(parents=True, exist_ok=True)
        data = contract.to_dict()
        version = data["version"]
        _write_json(self.directory / f"{contract.contract_id}@{version}.json", data)
        _write_json(self.directory / f"{contract.contract_id}@current.json", data)
        return data

    def fetch(self, contract_id: str, version: str = "current") -> dict[str, Any] | None:
        return _read_json(self.directory / f"{contract_id}@{version}.json")

    def versions(self, contract_id: str) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(
            p.stem.split("@", 1)[1]
            for p in self.directory.glob(f"{contract_id}@*.json")
            if not p.stem.endswith("@current")
        )

    def verify(self, contract: ExecutionContract) -> dict[str, Any]:
        """Check this node's rules against the fleet's published contract.

        Returns rather than raises when nothing is published: the first node
        to start has no fleet to disagree with, and refusing to run because
        nobody has published yet would make the system unbootable.
        """
        published = self.fetch(contract.contract_id)
        mine = contract.fingerprint()
        if published is None:
            return {"status": "unpublished", "version": mine,
                    "note": "no fleet contract yet; this node's rules stand alone"}
        theirs = str(published.get("version", ""))
        if theirs == mine:
            return {"status": "match", "version": mine}
        return {
            "status": "drift",
            "version": mine,
            "fleet_version": theirs,
            "detail": (
                f"this runtime enforces contract {mine} but the fleet is on {theirs}. "
                "A node running different rules from its peers is exactly the case "
                "contract distribution exists to catch — redeploy it, or publish this "
                "version deliberately."
            ),
        }


# --- 4. cross-runtime containment ------------------------------------------------


class CrossRuntimeGuard:
    """Which other runtimes this one may talk to.

    Agents on separate hosts must not be able to route around their own
    containment by asking a peer to act for them. The allowlist is empty by
    default: nodes coordinate through the shared registry and bus, not by
    calling each other.
    """

    def __init__(self, approved: set[str] | None = None) -> None:
        self.approved = approved or set()

    @classmethod
    def from_env(cls) -> "CrossRuntimeGuard":
        raw = os.environ.get("LUMIA_APPROVED_RUNTIMES", "")
        return cls({t.strip() for t in raw.split(",") if t.strip()})

    def permitted(self, destination: str) -> bool:
        return destination in self.approved

    def check(self, destination: str) -> None:
        if self.permitted(destination):
            return
        from .contract import OUT_OF_SCOPE_HOST, Breach, ContractBreach

        raise ContractBreach(Breach(
            kind=OUT_OF_SCOPE_HOST,
            attempted=destination,
            allowed=sorted(self.approved),
            detail=(
                f"cross-runtime call to '{destination}' blocked. Runtimes coordinate "
                "through the shared registry, not by calling each other — an agent that "
                "can ask a peer to act for it has no containment. Add it to "
                "LUMIA_APPROVED_RUNTIMES if this route is genuinely intended."
            ),
        ))


# --- 5. coordinated phase transitions ---------------------------------------------


class PhaseBarrier:
    """Hold a phase until the fleet is ready for it.

    The case that matters at Ashrah: when several agents work one project,
    nothing should send until everything has finished gathering. One agent
    sending a daily log while another is still processing the field report
    it depends on produces a client message that contradicts the record
    thirty seconds later.

    Membership is whatever the registry says is running in the cohort, so a
    runtime that dies mid-phase stops blocking once its entry goes stale
    rather than holding the fleet forever.
    """

    def __init__(self, registry: RuntimeRegistry, order: list[str]) -> None:
        self.registry = registry
        self.order = order

    def _rank(self, phase: str) -> int:
        return self.order.index(phase) if phase in self.order else -1

    def peers(self, cohort: str, exclude: str = "") -> list[dict[str, Any]]:
        return [
            entry for entry in self.registry.active()
            if entry.get("cohort") == cohort and entry.get("run") != exclude
        ]

    def blockers(self, cohort: str, phase: str, exclude: str = "") -> list[dict[str, Any]]:
        """Peers not yet far enough along for this phase to begin."""
        wanted = self._rank(phase)
        if wanted < 0:
            return []
        return [
            peer for peer in self.peers(cohort, exclude=exclude)
            if peer.get("status") != "paused" and 0 <= self._rank(str(peer.get("phase", ""))) < wanted
        ]

    def ready(self, cohort: str, phase: str, exclude: str = "") -> bool:
        return not self.blockers(cohort, phase, exclude=exclude)

    def wait(
        self,
        cohort: str,
        phase: str,
        exclude: str = "",
        timeout: float = 60.0,
        poll: float = 0.25,
    ) -> dict[str, Any]:
        """Block until the fleet is ready, or give up and say who was late.

        Never waits forever. A barrier that cannot time out turns one wedged
        runtime into a stalled fleet, which is worse than the inconsistency
        it was protecting against.
        """
        deadline = time.monotonic() + timeout
        while True:
            blocking = self.blockers(cohort, phase, exclude=exclude)
            if not blocking:
                return {"ready": True, "waited_seconds": round(timeout - (deadline - time.monotonic()), 2)}
            if time.monotonic() >= deadline:
                return {
                    "ready": False,
                    "timed_out": True,
                    "waiting_on": [
                        {"run": b.get("run"), "phase": b.get("phase"), "node": b.get("node")}
                        for b in blocking
                    ],
                    "note": "proceeding without the fleet; a stuck peer must not stall everyone",
                }
            time.sleep(poll)


# --- storage helpers -----------------------------------------------------------


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        json.dump(payload, handle, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


@dataclass
class Fleet:
    """The five L5 controls, rooted at one shared directory."""

    root: Path

    @property
    def registry(self) -> RuntimeRegistry:
        return RuntimeRegistry(self.root / "registry")

    @property
    def lifecycle(self) -> LifecycleBus:
        return LifecycleBus(self.root / "kill")   # shares the kill switch's path

    @property
    def contracts(self) -> ContractStore:
        return ContractStore(self.root / "contracts")

    @property
    def cross_runtime(self) -> CrossRuntimeGuard:
        return CrossRuntimeGuard.from_env()

    def barrier(self, order: list[str]) -> PhaseBarrier:
        return PhaseBarrier(self.registry, order)

    def status(self) -> dict[str, Any]:
        active = self.registry.active()
        return {
            "node": node_name(),
            "active_runs": len(active),
            "runs": active,
            "by_node": _count(active, "node"),
            "by_phase": _count(active, "phase"),
            "lifecycle_commands": self.lifecycle.outstanding(),
            "approved_runtimes": sorted(self.cross_runtime.approved),
        }


def _count(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = str(record.get(key, "unknown"))
        counts[value] = counts.get(value, 0) + 1
    return counts
