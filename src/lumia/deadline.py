"""Time budgets for a run.

Runs are **uncapped by default**. A time limit was tried and cancelled: a
communication cut off part-way leaves a client half-told and a crew
half-briefed, and an incomplete message costs more than a slow one. Runs
are stopped by an operator (see `observability.KillSwitch`), not by a
clock.

The machinery stays, because a budget is still the right tool sometimes —
a web request that must return, a scheduled job that must not overlap the
next one. Pass `budget_seconds` to a run, or set
`LUMIA_COMMS_BUDGET_SECONDS`, and every mechanism below applies.

When a budget *is* set, enforcement happens at three points, because a
single one is not enough:

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

#: No limit. A `Deadline` carrying this never expires, and the watchdog
#: stands down — there is nothing to be late for.
UNLIMITED: float | None = None


def _seconds(name: str, default: float | None) -> float | None:
    """A budget from the environment, or the default. Blank means unchanged.

    `0` is a real value meaning "already spent", distinct from `None`
    meaning "no limit" — a run given a zero budget stops immediately, which
    is a legitimate thing to ask for and is not the same as asking for no
    cap at all.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if raw.lower() in {"none", "off", "unlimited", "-1"}:
        return UNLIMITED
    try:
        return float(raw)
    except ValueError:
        return default


#: Communication runs are not time-capped. The 120-second limit that used to
#: sit here was cancelled: a run cut off mid-way leaves a client half-told
#: and a crew half-briefed, and that costs more than a slow message does.
#: Set LUMIA_COMMS_BUDGET_SECONDS to put a cap back.
COMMS_BUDGET_SECONDS = _seconds("LUMIA_COMMS_BUDGET_SECONDS", UNLIMITED)

#: Growth work is likewise uncapped by default; researching an account
#: properly is worth more than researching it quickly.
GROWTH_BUDGET_SECONDS = _seconds("LUMIA_GROWTH_BUDGET_SECONDS", UNLIMITED)

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

    budget_seconds: float | None = COMMS_BUDGET_SECONDS
    label: str = "communication"
    started: float = field(default_factory=time.monotonic)

    @property
    def limited(self) -> bool:
        return self.budget_seconds is not None

    @property
    def elapsed(self) -> float:
        return round(time.monotonic() - self.started, 2)

    @property
    def remaining(self) -> float | None:
        """Seconds left, or None when there is no limit."""
        if not self.limited:
            return None
        return round(float(self.budget_seconds) - self.elapsed, 2)

    @property
    def expired(self) -> bool:
        remaining = self.remaining
        return remaining is not None and remaining <= 0

    @property
    def usable(self) -> bool:
        """Whether there is enough left to be worth starting something."""
        remaining = self.remaining
        return remaining is None or remaining >= MIN_USEFUL_SECONDS

    def timeout_for(self, preferred: float) -> float:
        """The shorter of what a call wants and what the run has left."""
        remaining = self.remaining
        if remaining is None:
            return preferred
        return max(0.1, min(preferred, remaining))

    def check(self, what: str = "") -> None:
        if self.expired:
            raise DeadlineExceeded(
                f"the {self.label} budget of {float(self.budget_seconds):.0f}s ran out"
                + (f" before {what}" if what else "")
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_seconds": self.budget_seconds,
            "limited": self.limited,
            "elapsed_seconds": self.elapsed,
            "remaining_seconds": self.remaining,
            "expired": self.expired,
        }


def budget_for(role: str, comms_roles: tuple[str, ...] = ()) -> float | None:
    """The default budget for one agent's work, or None for no limit."""
    return COMMS_BUDGET_SECONDS if role in comms_roles else GROWTH_BUDGET_SECONDS
