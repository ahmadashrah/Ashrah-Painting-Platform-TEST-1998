"""The contract a run operates under, and what happens when it is broken.

A run is allowed to do a bounded set of things: the tools its phase holds,
and network calls to the services this deployment is configured for.
Reaching outside that is not a mistake to be corrected mid-flight — it is
the run doing something nobody authorized, and the safe response is to stop
it immediately and leave the evidence.

**What counts as a breach**

- A tool the current phase does not hold.
- A tool that does not exist, or that the role never held.
- An HTTP request to a host this deployment is not configured to talk to.

**What deliberately does not**

Being *gated* is not a breach. A Level 3 tool queued for approval is the
harness working exactly as designed, and killing the run for it would
punish the agent for using the approval path correctly. The distinction is
the whole point: a gated action is a request to a human, a breach is a
step around one.

Nor is a tool *error*. A tool that runs and returns "no project with that
id" was reached legitimately; the agent should see the error and recover.

**Why killing rather than refusing**

Refusing tells the agent "not that one" and lets it try again, which is
the right response to a wrong guess and the wrong response to an attempt
to act outside the contract. Once a run has tried to step outside its
scope, nothing it does afterwards can be trusted to be within it, and the
cheapest safe state is stopped. `LUMIA_ON_BREACH=refuse` restores the old
behaviour where that trade is not wanted.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: What to do when a run steps outside its contract.
KILL = "kill"
REFUSE = "refuse"


def policy() -> str:
    choice = os.environ.get("LUMIA_ON_BREACH", KILL).strip().lower()
    return REFUSE if choice == REFUSE else KILL


#: Breach kinds, for the record and for the operator's window.
OUT_OF_PHASE = "tool_out_of_phase"
UNKNOWN_TOOL = "unknown_tool"
OUT_OF_SCOPE_HOST = "out_of_scope_host"


@dataclass
class Breach:
    """One attempt to act outside the contract."""

    kind: str
    detail: str
    attempted: str = ""
    phase: str = ""
    allowed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "attempted": self.attempted,
            "phase": self.phase,
            "allowed": self.allowed,
        }

    def __str__(self) -> str:
        return self.detail


class ContractBreach(RuntimeError):
    """Raised to stop a run that tried to act outside its contract.

    Carries the breach so the runner can record what was attempted rather
    than only that something was.
    """

    def __init__(self, breach: Breach) -> None:
        super().__init__(breach.detail)
        self.breach = breach


# --- network scope ------------------------------------------------------------


#: Hosts the platform always needs, independent of which integrations are on.
CORE_HOSTS = ("api.anthropic.com", "api.openai.com")


class EgressGuard:
    """Decides whether a run may make a given HTTP request.

    The agent supplies URLs — a photo's location, a recording to transcribe
    — and those are fetched. Without a check, "transcribe this audio" is a
    request to fetch any address the model can name, from inside the
    company's network. This is the check.

    The allowlist is built from what the deployment is actually configured
    for: the base URL of every service with credentials, plus the model
    providers, plus anything named explicitly in `LUMIA_ALLOWED_HOSTS`.
    Media lives on hosts that vary by customer, so those are added
    deliberately rather than guessed.
    """

    def __init__(self, hosts: set[str] | None = None) -> None:
        self.hosts = hosts or set()

    @classmethod
    def from_settings(cls, settings: Any) -> "EgressGuard":
        hosts = set(CORE_HOSTS)
        for credentials in getattr(settings, "services", {}).values():
            host = _host_of(str(getattr(credentials, "base_url", "") or ""))
            if host:
                hosts.add(host)
        for extra in os.environ.get("LUMIA_ALLOWED_HOSTS", "").split(","):
            host = extra.strip().lower()
            if host:
                hosts.add(host)
        return cls(hosts)

    def permitted(self, url: str) -> bool:
        host = _host_of(url)
        if not host:
            return False
        if host in self.hosts:
            return True
        # A subdomain of an allowed host is allowed; a host merely ending in
        # the same characters is not. "evil-example.com" must not pass for
        # "example.com".
        return any(host.endswith("." + allowed) for allowed in self.hosts)

    def check(self, url: str, what: str = "") -> None:
        if self.permitted(url):
            return
        host = _host_of(url) or "an unparseable address"
        raise ContractBreach(Breach(
            kind=OUT_OF_SCOPE_HOST,
            attempted=host,
            allowed=sorted(self.hosts),
            detail=(
                f"the run tried to reach {host}"
                + (f" while {what}" if what else "")
                + ", which this deployment is not configured to talk to. "
                "Add it to LUMIA_ALLOWED_HOSTS if it is genuinely yours."
            ),
        ))


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


# --- the contract this runtime is enforcing ------------------------------------


def current_contract(settings: Any, contract_id: str = "lumia") -> Any:
    """Build the contract from the rules this process actually enforces.

    Derived from the live tables rather than written down beside them. A
    contract maintained by hand drifts from the code it describes, and a
    fleet comparing hand-maintained descriptions would agree precisely when
    it should not.
    """
    from .autonomy import AUTO_SENDABLE_KINDS, TOOL_LEVELS
    from .fleet import ExecutionContract
    from .phases import PLANS

    return ExecutionContract(
        contract_id=contract_id,
        tool_levels={name: int(level) for name, level in sorted(TOOL_LEVELS.items())},
        auto_sendable_kinds=sorted(AUTO_SENDABLE_KINDS),
        phase_plans={
            role: [{"name": p.name, "tools": sorted(p.tools)} for p in phases]
            for role, phases in sorted(PLANS.items())
        },
        allowed_hosts=sorted(EgressGuard.from_settings(settings).hosts),
        on_breach=policy(),
    )
