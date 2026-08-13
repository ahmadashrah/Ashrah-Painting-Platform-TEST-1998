"""Time budgets for a run.

Communication is time-critical in a way the growth side is not. A crew is
standing at a locked door, a client is waiting on the log before they leave
site, a supplier's cut-off is in ten minutes. A message that arrives late
has partly failed even if the wording was perfect, so the whole path —
drafting, ordering paint, sending a log, dispatching a crew — carries a
hard budget rather than a hope.

Enforcement happens at three points, because a single one is not enough:

1. **Between turns.** The loop refuses to start another model call once the
   budget is gone.
2. **Before a tool call.** No new outbound work begins on a spent budget.
3. **Inside an HTTP call.** Each request is given what is actually left,
   and retries stop when there is no room for another attempt. Without
   this, one slow provider with three retries can eat half the budget on
   its own.

What is deliberately *not* done is aborting a request already in flight. A
send that is cancelled mid-write may still have been delivered, and a
message the record shows as unsent but the client received is a worse
outcome than a message that finishes four seconds late. The budget stops
new work; it does not interrupt work that is already committed.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

def _seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


#: Anything a person is waiting on. The spec's classes of communication —
#: ordering materials, sending a log, assigning an employee, drafting a
#: message — all live under this.
COMMS_BUDGET_SECONDS = _seconds("LUMIA_COMMS_BUDGET_SECONDS", 120.0)

#: Growth work is not time-critical in the same way; researching an account
#: properly is worth more than researching it quickly.
GROWTH_BUDGET_SECONDS = _seconds("LUMIA_GROWTH_BUDGET_SECONDS", 600.0)

#: Below this there is no point starting another network call — it would
#: only fail slower than refusing does.
MIN_USEFUL_SECONDS = 1.5


class DeadlineExceeded(RuntimeError):
    """Raised when work is attempted on a spent budget."""


@dataclass
class Deadline:
    """A shrinking budget, measured on the monotonic clock.

    Monotonic rather than wall clock deliberately: an NTP correction or a
    daylight-saving jump must not hand a run an extra hour or cut it short.
    """

    budget_seconds: float = COMMS_BUDGET_SECONDS
    label: str = "communication"
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return round(time.monotonic() - self.started, 2)

    @property
    def remaining(self) -> float:
        return round(self.budget_seconds - self.elapsed, 2)

    @property
    def expired(self) -> bool:
        return self.remaining <= 0

    @property
    def usable(self) -> bool:
        """Whether there is enough left to be worth starting something."""
        return self.remaining >= MIN_USEFUL_SECONDS

    def timeout_for(self, preferred: float) -> float:
        """The shorter of what a call wants and what the run has left."""
        return max(0.1, min(preferred, self.remaining))

    def check(self, what: str = "") -> None:
        if self.expired:
            raise DeadlineExceeded(
                f"the {self.label} budget of {self.budget_seconds:.0f}s ran out"
                + (f" before {what}" if what else "")
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_seconds": self.budget_seconds,
            "elapsed_seconds": self.elapsed,
            "remaining_seconds": self.remaining,
            "expired": self.expired,
        }


def budget_for(role: str, comms_roles: tuple[str, ...] = ()) -> float:
    """The default budget for one agent's work."""
    return COMMS_BUDGET_SECONDS if role in comms_roles else GROWTH_BUDGET_SECONDS
