"""Projects, production phases and the dependencies between them.

A painting project is not one block of hours. It is an ordered set of
phases with real dependencies: you cannot mask before you patch, you cannot
top-coat inside a recoat window, and you cannot walk a client through wet
paint. Encoding that here means the agent schedules against the sequence
rather than re-deriving it — and cannot quietly plan a final coat for the
same afternoon as the first.

Priority is also code, not judgement. The spec gives an explicit ordering;
`ProjectPriority` is that ordering, so two projects competing for one crew
resolve the same way every time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum, IntEnum
from typing import Any

from .accounts import new_id, today_iso
from .models import _serialize


class ProjectStatus(str, Enum):
    TENTATIVE = "tentative"       # not confirmed — never staff at the expense of confirmed work
    CONFIRMED = "confirmed"
    ACTIVE = "active"
    ON_HOLD = "on_hold"
    DEFICIENCY = "deficiency"     # substantially complete, punch list outstanding
    COMPLETE = "complete"
    CANCELLED = "cancelled"


#: Statuses that still need crew.
SCHEDULABLE_STATUSES = {
    ProjectStatus.CONFIRMED.value,
    ProjectStatus.ACTIVE.value,
    ProjectStatus.DEFICIENCY.value,
    ProjectStatus.TENTATIVE.value,
}


class ProjectPriority(IntEnum):
    """The scheduling priority order, lowest number first.

    This is the spec's list encoded directly. Management instructions can
    override an individual project's priority, but the ordering itself is
    fixed so that ties never resolve by whim.
    """

    SAFETY = 1                 # immediate health and safety
    CONTRACT_DEADLINE = 2      # confirmed contractual deadline
    CRITICAL_PATH = 3          # on the project critical path
    CLIENT_COMMITMENT = 4      # promised to a client or GC
    TRADE_BLOCKING = 5         # another trade waits on us
    BUDGET_RISK = 6            # at risk of a labour-budget overrun
    STRATEGIC = 7              # high-value or strategically important
    DEFICIENCY = 8             # deficiencies, callbacks, warranty
    TENTATIVE = 9              # unconfirmed


class Phase(str, Enum):
    """The production phases, in the spec's order."""

    MOBILIZATION = "mobilization"
    INSPECTION = "inspection"
    PREP = "prep"
    MASKING = "masking"
    PRIMER = "primer"
    CEILINGS = "ceilings"
    WALLS_FIRST_COAT = "walls_first_coat"
    TRIM_AND_DOORS = "trim_and_doors"
    WALLS_FINAL_COAT = "walls_final_coat"
    SPECIALTY_COATING = "specialty_coating"
    DEFICIENCIES = "deficiencies"
    WALKTHROUGH = "walkthrough"
    DEMOBILIZATION = "demobilization"


@dataclass(frozen=True)
class PhaseSpec:
    """How one phase behaves: what it needs, what it blocks, how long it cures."""

    phase: Phase
    sequence: int
    #: Share of total labour hours when the phase is in scope.
    share: float
    #: Phases that must be complete before this one can start.
    depends_on: tuple[Phase, ...] = ()
    #: Hours that must elapse after this phase before a dependent phase starts.
    cure_hours: float = 0.0
    #: Skills this phase actually needs.
    skills: tuple[str, ...] = ()
    #: True when the phase is a coating application, so ambient conditions apply.
    is_coating: bool = False
    #: Only scheduled when the project scope includes it.
    optional: bool = False


#: The standard production plan. Shares sum to 1.0 across non-optional phases.
PHASE_PLAN: tuple[PhaseSpec, ...] = (
    PhaseSpec(Phase.MOBILIZATION, 1, 0.04, skills=("surface_prep",)),
    PhaseSpec(Phase.INSPECTION, 2, 0.02, depends_on=(Phase.MOBILIZATION,), skills=("colour_matching",)),
    PhaseSpec(Phase.PREP, 3, 0.22, depends_on=(Phase.INSPECTION,), skills=("surface_prep", "drywall_repair")),
    PhaseSpec(Phase.MASKING, 4, 0.10, depends_on=(Phase.PREP,), skills=("surface_prep",)),
    PhaseSpec(
        Phase.PRIMER, 5, 0.10, depends_on=(Phase.MASKING,), cure_hours=4.0,
        skills=("spray", "back_roll"), is_coating=True,
    ),
    PhaseSpec(
        Phase.CEILINGS, 6, 0.10, depends_on=(Phase.PRIMER,), cure_hours=4.0,
        skills=("spray",), is_coating=True,
    ),
    PhaseSpec(
        Phase.WALLS_FIRST_COAT, 7, 0.12, depends_on=(Phase.CEILINGS,), cure_hours=4.0,
        skills=("cut_and_roll", "spray"), is_coating=True,
    ),
    PhaseSpec(
        Phase.TRIM_AND_DOORS, 8, 0.12, depends_on=(Phase.WALLS_FIRST_COAT,), cure_hours=4.0,
        skills=("fine_finish", "wood_finishing"), is_coating=True,
    ),
    PhaseSpec(
        Phase.WALLS_FINAL_COAT, 9, 0.10, depends_on=(Phase.WALLS_FIRST_COAT,), cure_hours=16.0,
        skills=("cut_and_roll", "fine_finish"), is_coating=True,
    ),
    PhaseSpec(
        Phase.SPECIALTY_COATING, 10, 0.10, depends_on=(Phase.WALLS_FINAL_COAT,), cure_hours=24.0,
        skills=("epoxy_floor",), is_coating=True, optional=True,
    ),
    PhaseSpec(Phase.DEFICIENCIES, 11, 0.05, depends_on=(Phase.WALLS_FINAL_COAT,), skills=("fine_finish",)),
    PhaseSpec(
        Phase.WALKTHROUGH, 12, 0.01, depends_on=(Phase.DEFICIENCIES,),
        skills=("client_facing",),
    ),
    PhaseSpec(Phase.DEMOBILIZATION, 13, 0.02, depends_on=(Phase.WALKTHROUGH,)),
)

PHASE_SPECS: dict[str, PhaseSpec] = {spec.phase.value: spec for spec in PHASE_PLAN}


class PhaseStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    NOT_IN_SCOPE = "not_in_scope"


@dataclass
class ProjectPhase:
    project_id: str
    phase: Phase
    sequence: int
    estimated_hours: float
    depends_on: list[str] = field(default_factory=list)
    cure_hours: float = 0.0
    status: PhaseStatus = PhaseStatus.PENDING
    completed_pct: float = 0.0
    actual_hours: float = 0.0
    planned_start: str = ""
    planned_end: str = ""
    id: str = field(default_factory=lambda: new_id("phs"))

    @property
    def remaining_hours(self) -> float:
        return round(max(self.estimated_hours * (1.0 - self.completed_pct / 100.0), 0.0), 2)

    def to_dict(self) -> dict[str, Any]:
        data = _serialize(self)
        data["remaining_hours"] = self.remaining_hours
        return data


@dataclass
class Project:
    name: str
    address: str = ""
    city: str = ""                          # drives the weather lookup
    client_name: str = ""
    client_email: str = ""
    client_phone: str = ""
    #: Links delivery back to the CRM account that won the work.
    account_id: str = ""
    status: ProjectStatus = ProjectStatus.TENTATIVE
    priority: ProjectPriority = ProjectPriority.CLIENT_COMMITMENT
    start_date: str = ""
    deadline: str = ""
    estimated_hours: float = 0.0
    labour_budget_hours: float = 0.0
    actual_hours: float = 0.0
    crew_size_target: int = 2
    required_skills: list[str] = field(default_factory=list)
    required_certifications: list[str] = field(default_factory=list)
    includes_specialty_coating: bool = False
    is_exterior: bool = False
    is_occupied: bool = False               # occupants on site constrain hours and noise
    site_access_start: str = "07:00"
    site_access_end: str = "17:00"
    access_notes: str = ""
    blocking_trade: str = ""                # another trade that must finish first
    scope_notes: str = ""
    id: str = field(default_factory=lambda: new_id("proj"))
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def hours_remaining(self) -> float:
        return round(max(self.estimated_hours - self.actual_hours, 0.0), 2)

    def days_to_deadline(self, on: date | None = None) -> int | None:
        if not self.deadline:
            return None
        try:
            return (date.fromisoformat(self.deadline) - (on or date.today())).days
        except ValueError:
            return None

    def to_dict(self) -> dict[str, Any]:
        data = _serialize(self)
        data["hours_remaining"] = self.hours_remaining
        return data


def phase_plan(project: Project | dict[str, Any]) -> list[ProjectPhase]:
    """Break a project's labour estimate into its production phases.

    Shares are renormalized over the phases actually in scope, so a project
    with epoxy work does not silently inflate past its estimate.
    """
    record = project.to_dict() if isinstance(project, Project) else dict(project)
    project_id = str(record.get("id", ""))
    total_hours = float(record.get("estimated_hours", 0) or 0)
    include_specialty = bool(record.get("includes_specialty_coating", False))

    in_scope = [s for s in PHASE_PLAN if not s.optional or include_specialty]
    share_total = sum(s.share for s in in_scope) or 1.0

    phases: list[ProjectPhase] = []
    for spec in in_scope:
        phases.append(
            ProjectPhase(
                project_id=project_id,
                phase=spec.phase,
                sequence=spec.sequence,
                estimated_hours=round(total_hours * spec.share / share_total, 2),
                depends_on=[p.value for p in spec.depends_on if p.value in {s.phase.value for s in in_scope}],
                cure_hours=spec.cure_hours,
            )
        )
    return phases


def next_schedulable_phases(phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Phases whose dependencies are met — the only ones worth staffing.

    A phase whose predecessor is unfinished is returned as blocked, with the
    reason, so the caller can report *why* work cannot start rather than
    silently skipping it.
    """
    by_phase = {str(p.get("phase", "")): p for p in phases}
    ready: list[dict[str, Any]] = []

    for record in sorted(phases, key=lambda p: int(p.get("sequence", 0) or 0)):
        status = str(record.get("status", PhaseStatus.PENDING.value))
        if status in {PhaseStatus.COMPLETE.value, PhaseStatus.NOT_IN_SCOPE.value}:
            continue

        blockers: list[str] = []
        for dependency in record.get("depends_on", []) or []:
            upstream = by_phase.get(str(dependency))
            if upstream is None:
                continue
            if str(upstream.get("status", "")) != PhaseStatus.COMPLETE.value:
                blockers.append(
                    f"{dependency} is {upstream.get('status', 'pending')} "
                    f"({float(upstream.get('completed_pct', 0) or 0):.0f}% complete)"
                )

        entry = dict(record)
        entry["blocked_by"] = blockers
        entry["schedulable"] = not blockers
        ready.append(entry)

    return ready


def cure_gap_days(phase_name: str) -> int:
    """Calendar days a cure window costs before the next phase can start.

    Anything up to a normal overnight (16h) is absorbed by the shift change
    and costs no day; longer windows cost one.
    """
    spec = PHASE_SPECS.get(phase_name)
    if spec is None or spec.cure_hours <= 16.0:
        return 0
    return max(1, int(spec.cure_hours // 24) or 1)


def priority_of(project: dict[str, Any]) -> int:
    """Sort key for competing projects: lower wins."""
    try:
        return int(project.get("priority", ProjectPriority.CLIENT_COMMITMENT))
    except (TypeError, ValueError):
        return int(ProjectPriority.CLIENT_COMMITMENT)


def deadline_pressure(project: dict[str, Any], on: date | None = None) -> float:
    """Remaining hours per remaining working day. Above ~8 per crew member is trouble."""
    today = on or date.today()
    deadline = str(project.get("deadline", "") or "")
    remaining = float(project.get("hours_remaining", project.get("estimated_hours", 0)) or 0)
    if not deadline or remaining <= 0:
        return 0.0
    try:
        end = date.fromisoformat(deadline)
    except ValueError:
        return 0.0

    days = 0
    cursor = today
    while cursor <= end:
        if cursor.weekday() < 5:
            days += 1
        cursor += timedelta(days=1)
    if days <= 0:
        return float("inf")
    return round(remaining / days, 2)


__all__ = [
    "PHASE_PLAN",
    "PHASE_SPECS",
    "SCHEDULABLE_STATUSES",
    "Phase",
    "PhaseSpec",
    "PhaseStatus",
    "Project",
    "ProjectPhase",
    "ProjectPriority",
    "ProjectStatus",
    "cure_gap_days",
    "deadline_pressure",
    "next_schedulable_phases",
    "phase_plan",
    "priority_of",
    "today_iso",
]
