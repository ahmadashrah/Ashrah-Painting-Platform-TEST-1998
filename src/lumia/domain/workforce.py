"""People, availability, and the record of who is working where.

The scheduling spec's hard rules — never double-book, never ignore approved
time off, never assign uncertified labour to specialized work — are only
real if they are checkable. That means availability and qualification have
to be data, not prose. This module is that data.

Skills and certifications are deliberately job-relevant and evidence-based.
Nothing here records a protected personal characteristic, and nothing here
is a permanent label: levels and reliability are numbers that move as
evidence arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum, IntEnum
from typing import Any

from .accounts import new_id, today_iso
from .models import _serialize


class CrewRole(str, Enum):
    """What someone is employed to do, not how good they are at it."""

    APPRENTICE = "apprentice"
    PAINTER = "painter"
    SENIOR_PAINTER = "senior_painter"
    LEAD = "lead"
    SUPERVISOR = "supervisor"
    SUBCONTRACTOR = "subcontractor"


class SkillLevel(IntEnum):
    """Assessed capability on one trade skill."""

    NONE = 0
    LEARNING = 1        # needs supervision
    COMPETENT = 2       # works unsupervised on routine scope
    STRONG = 3          # works unsupervised on demanding scope
    EXPERT = 4          # sets the standard, can train others


#: The trade skills scheduling reasons about, from the assignment rules.
SKILLS: tuple[str, ...] = (
    "surface_prep",
    "cut_and_roll",
    "spray",
    "back_roll",
    "fine_finish",
    "drywall_repair",
    "wood_finishing",
    "epoxy_floor",
    "exterior",
    "wallcovering",
    "colour_matching",
    "client_facing",
)

#: Certifications that gate work. Absent or expired means not qualified.
CERTIFICATIONS: tuple[str, ...] = (
    "working_at_heights",
    "fall_protection",
    "boom_lift",
    "scissor_lift",
    "whmis",
    "first_aid",
    "confined_space",
    "respirator_fit",
)

#: A certification inside this window is still valid but must be flagged.
CERT_EXPIRY_WARNING_DAYS = 45


class TimeOffKind(str, Enum):
    VACATION = "vacation"
    SICK = "sick"
    APPOINTMENT = "appointment"
    UNPAID = "unpaid"
    STATUTORY = "statutory"
    TRAINING = "training"


class TimeOffStatus(str, Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"


class AssignmentStatus(str, Enum):
    """A draft is a proposal. Only `confirmed` may be described as scheduled."""

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


#: Assignments that actually occupy someone's day.
ACTIVE_ASSIGNMENT_STATUSES = {
    AssignmentStatus.DRAFT.value,
    AssignmentStatus.CONFIRMED.value,
    AssignmentStatus.COMPLETED.value,
}


# --- working-hour limits -------------------------------------------------
#
# Two different kinds of limit. The safety caps are absolute and are enforced
# as hard blocks. The overtime threshold is a policy line: crossing it is
# allowed, but only with management approval.

STANDARD_HOURS_PER_DAY = 8.0
MAX_HOURS_PER_DAY = 12.0            # hard cap — fatigue and safety
STANDARD_HOURS_PER_WEEK = 40.0
OVERTIME_THRESHOLD_HOURS_PER_WEEK = 44.0   # beyond this needs approval
MAX_HOURS_PER_WEEK = 60.0           # hard cap


@dataclass
class Employee:
    name: str
    crew_role: CrewRole = CrewRole.PAINTER
    phone: str = ""
    email: str = ""
    #: skill name -> SkillLevel value. Missing means NONE.
    skills: dict[str, int] = field(default_factory=dict)
    #: certification name -> ISO expiry date. "" means it does not expire.
    certifications: dict[str, str] = field(default_factory=dict)
    #: Output relative to the planning baseline. 1.0 is on-standard.
    productivity_factor: float = 1.0
    #: Attendance and punctuality, 0-1, derived from clock-in history.
    reliability: float = 1.0
    can_lead: bool = False
    works_alone: bool = True
    home_base: str = ""
    max_hours_per_week: float = OVERTIME_THRESHOLD_HOURS_PER_WEEK
    active: bool = True
    #: Documented, reviewable coaching needs — never a permanent judgement.
    development_notes: str = ""
    notes: str = ""
    id: str = field(default_factory=lambda: new_id("emp"))
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def skill(self, name: str) -> int:
        return int(self.skills.get(name, 0) or 0)

    def certified_for(self, certification: str, on: date | None = None) -> bool:
        """True when the certification is held and not expired on that date."""
        if certification not in self.certifications:
            return False
        expiry = str(self.certifications[certification] or "").strip()
        if not expiry:
            return True
        try:
            return date.fromisoformat(expiry) >= (on or date.today())
        except ValueError:
            return False

    def missing_certifications(self, required: list[str], on: date | None = None) -> list[str]:
        return [c for c in required if not self.certified_for(c, on)]

    def expiring_certifications(self, on: date | None = None, within_days: int = CERT_EXPIRY_WARNING_DAYS) -> list[dict[str, Any]]:
        """Certifications expiring soon or already expired — a monitoring input."""
        today = on or date.today()
        horizon = today + timedelta(days=within_days)
        found: list[dict[str, Any]] = []
        for name, expiry in self.certifications.items():
            if not str(expiry or "").strip():
                continue
            try:
                expires = date.fromisoformat(str(expiry))
            except ValueError:
                continue
            if expires <= horizon:
                found.append(
                    {
                        "certification": name,
                        "expires_on": expires.isoformat(),
                        "days_remaining": (expires - today).days,
                        "expired": expires < today,
                    }
                )
        return sorted(found, key=lambda c: c["expires_on"])

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass
class TimeOff:
    """Approved time off is a hard constraint. Requested time off is a risk."""

    employee_id: str
    start_date: str                     # ISO, inclusive
    end_date: str                       # ISO, inclusive
    kind: TimeOffKind = TimeOffKind.VACATION
    status: TimeOffStatus = TimeOffStatus.APPROVED
    note: str = ""
    id: str = field(default_factory=lambda: new_id("tmo"))
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def covers(self, day: date) -> bool:
        try:
            first = date.fromisoformat(self.start_date)
            last = date.fromisoformat(self.end_date or self.start_date)
        except ValueError:
            return False
        return first <= day <= last

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass
class Assignment:
    """One person, one project, one day."""

    project_id: str
    employee_id: str
    work_date: str                      # ISO
    phase: str = ""
    hours: float = STANDARD_HOURS_PER_DAY
    is_lead: bool = False
    status: AssignmentStatus = AssignmentStatus.DRAFT
    #: Why this person on this project — kept for the audit trail.
    rationale: str = ""
    #: The approval id when this assignment needed a management exception.
    approval_id: str = ""
    notes: str = ""
    id: str = field(default_factory=lambda: new_id("asg"))
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass
class TimeEntry:
    """A clock-in/clock-out pair. Recorded, never invented."""

    employee_id: str
    work_date: str
    clock_in: str = ""                  # "07:04"
    clock_out: str = ""
    project_id: str = ""
    source: str = "timeclock"
    id: str = field(default_factory=lambda: new_id("tme"))

    @property
    def hours(self) -> float:
        start, end = _minutes(self.clock_in), _minutes(self.clock_out)
        if start is None or end is None or end <= start:
            return 0.0
        return round((end - start) / 60.0, 2)

    def to_dict(self) -> dict[str, Any]:
        data = _serialize(self)
        data["hours"] = self.hours
        return data


@dataclass
class Equipment:
    """Sprayers, lifts, sanders and vehicles — finite, and often the real constraint."""

    name: str
    kind: str = "other"                 # sprayer | lift | sander | vehicle | compressor | other
    active: bool = True
    notes: str = ""
    id: str = field(default_factory=lambda: new_id("eqp"))

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass
class EquipmentBooking:
    equipment_id: str
    project_id: str
    start_date: str
    end_date: str
    id: str = field(default_factory=lambda: new_id("eqb"))

    def overlaps(self, start: date, end: date) -> bool:
        try:
            first = date.fromisoformat(self.start_date)
            last = date.fromisoformat(self.end_date or self.start_date)
        except ValueError:
            return False
        return first <= end and start <= last

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


def _minutes(clock: str) -> int | None:
    """'07:30' -> 450. Returns None for anything unparseable."""
    text = str(clock or "").strip()
    if not text or ":" not in text:
        return None
    hh, _, mm = text.partition(":")
    try:
        hours, mins = int(hh), int(mm[:2])
    except ValueError:
        return None
    if not (0 <= hours <= 23 and 0 <= mins <= 59):
        return None
    return hours * 60 + mins


def week_start(day: date) -> date:
    """Monday of the week containing `day` — the overtime accounting window."""
    return day - timedelta(days=day.weekday())


def working_days(start: date, count: int, include_weekends: bool = False) -> list[date]:
    """The next `count` working days starting at `start`."""
    days: list[date] = []
    cursor = start
    guard = 0
    while len(days) < count and guard < count * 4 + 14:
        if include_weekends or cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
        guard += 1
    return days


__all__ = [
    "ACTIVE_ASSIGNMENT_STATUSES",
    "CERTIFICATIONS",
    "CERT_EXPIRY_WARNING_DAYS",
    "MAX_HOURS_PER_DAY",
    "MAX_HOURS_PER_WEEK",
    "OVERTIME_THRESHOLD_HOURS_PER_WEEK",
    "SKILLS",
    "STANDARD_HOURS_PER_DAY",
    "STANDARD_HOURS_PER_WEEK",
    "Assignment",
    "AssignmentStatus",
    "CrewRole",
    "Employee",
    "Equipment",
    "EquipmentBooking",
    "SkillLevel",
    "TimeEntry",
    "TimeOff",
    "TimeOffKind",
    "TimeOffStatus",
    "today_iso",
    "week_start",
    "working_days",
]
