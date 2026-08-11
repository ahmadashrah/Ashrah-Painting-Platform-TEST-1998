"""The scheduling engine.

This module is where the operating spec's "you must never" list becomes
mechanism. The rules that matter — never double-book, never ignore approved
time off, never assign uncertified labour to specialized work, never exceed
safe working hours — are checked here, in code, before anything is written.

Three tiers of verdict, and the distinction is the whole design:

- **Blocks** are absolute. Double-booking someone is not a trade-off a
  manager gets to make; there is no approval path and no override.
- **Exceptions** are allowed but not by the agent. Working someone on an
  approved day off, assigning past a certification, or authorizing overtime
  are real management decisions, so they route to the approval queue with
  the reasoning attached.
- **Warnings** are judgement. They travel with the result so the decision is
  informed, and they never silently stop work.

Nothing in this module writes. It computes verdicts and plans; `tools.py`
performs the writes, which keeps the autonomy gate the only door.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .domain.crewing import assemble_crew, requires_lead
from .domain.projects import (
    SCHEDULABLE_STATUSES,
    PhaseStatus,
    ProjectStatus,
    deadline_pressure,
    next_schedulable_phases,
    priority_of,
)
from .domain.workforce import (
    MAX_HOURS_PER_DAY,
    MAX_HOURS_PER_WEEK,
    OVERTIME_THRESHOLD_HOURS_PER_WEEK,
    STANDARD_HOURS_PER_DAY,
    AssignmentStatus,
    CrewRole,
    Employee,
    SkillLevel,
    TimeOffStatus,
    week_start,
    working_days,
)
from .workspace import Workspace

#: A draft shift this close to its work date has effectively become the plan.
DRAFT_CONFIRMATION_LEAD_DAYS = 2

#: Flag a project this far out if it still has no confirmed crew.
UNSTAFFED_WARNING_DAYS = 7

#: Labour budget consumed beyond this fraction is a live overrun risk.
BUDGET_WARNING_FRACTION = 0.9


# --- constraint checking ---------------------------------------------------


@dataclass
class ConstraintCheck:
    """The verdict on one proposed assignment."""

    blocks: list[str] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        """True when the assignment may be written without a human."""
        return not self.blocks and not self.exceptions

    @property
    def needs_approval(self) -> bool:
        return not self.blocks and bool(self.exceptions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "needs_management_approval": self.needs_approval,
            "blocks": self.blocks,
            "exceptions": self.exceptions,
            "warnings": self.warnings,
            "facts": self.facts,
        }


def check_assignment(
    ws: Workspace,
    project_id: str,
    employee_id: str,
    work_date: date,
    hours: float = STANDARD_HOURS_PER_DAY,
    *,
    excluding_assignment: str = "",
) -> ConstraintCheck:
    """Decide whether one person may be put on one project for one day."""
    check = ConstraintCheck()
    project = ws.ops.get_project(project_id)
    record = ws.ops.get_employee(employee_id)

    if project is None:
        check.blocks.append(f"No project with id {project_id}.")
    if record is None:
        check.blocks.append(f"No employee with id {employee_id}.")
    if project is None or record is None:
        return check

    employee = to_employee(record)
    check.facts = {
        "project": project.get("name", ""),
        "employee": employee.name,
        "work_date": work_date.isoformat(),
        "hours": hours,
    }

    # --- hard blocks ---------------------------------------------------

    status = str(project.get("status", ""))
    if status in {ProjectStatus.COMPLETE.value, ProjectStatus.CANCELLED.value}:
        check.blocks.append(f"Project is {status}; it cannot take new shifts.")

    if not employee.active:
        check.blocks.append(f"{employee.name} is not active on the roster.")

    if hours <= 0:
        check.blocks.append("Shift hours must be greater than zero.")

    same_day = [
        a for a in ws.ops.assignments_on(work_date, employee_id=employee_id)
        if a.get("id") != excluding_assignment
    ]
    other_projects = {
        str(a.get("project_id", "")) for a in same_day if str(a.get("project_id", "")) != project_id
    }
    if other_projects:
        names = ", ".join(
            str((ws.ops.get_project(p) or {}).get("name", p)) for p in sorted(other_projects)
        )
        check.blocks.append(
            f"{employee.name} is already scheduled on {names} on {work_date.isoformat()}. "
            "Double-booking is never an acceptable resolution — free the day first."
        )
    if any(str(a.get("project_id", "")) == project_id for a in same_day):
        check.blocks.append(
            f"{employee.name} already has a shift on this project on {work_date.isoformat()}."
        )

    day_hours = sum(float(a.get("hours", 0) or 0) for a in same_day) + hours
    if day_hours > MAX_HOURS_PER_DAY:
        check.blocks.append(
            f"{day_hours:.1f}h on {work_date.isoformat()} exceeds the {MAX_HOURS_PER_DAY:.0f}h "
            "daily safety cap."
        )

    week_hours = ws.ops.scheduled_hours_in_week(employee_id, work_date, excluding=excluding_assignment)
    projected_week = week_hours + hours
    check.facts["week_hours_before"] = week_hours
    check.facts["week_hours_after"] = round(projected_week, 2)
    if projected_week > MAX_HOURS_PER_WEEK:
        check.blocks.append(
            f"{projected_week:.1f}h in the week of {week_start(work_date).isoformat()} exceeds the "
            f"{MAX_HOURS_PER_WEEK:.0f}h weekly safety cap."
        )

    # --- management exceptions ------------------------------------------

    for record_off in ws.ops.time_off_for(employee_id):
        start = str(record_off.get("start_date", ""))
        end = str(record_off.get("end_date", "") or start)
        if not (start and start <= work_date.isoformat() <= end):
            continue
        if record_off.get("status") == TimeOffStatus.APPROVED.value:
            check.exceptions.append(
                f"{employee.name} has approved {record_off.get('kind', 'time off')} on "
                f"{work_date.isoformat()} ({start} to {end}). Scheduling over approved time off "
                "requires management approval."
            )
        elif record_off.get("status") == TimeOffStatus.REQUESTED.value:
            check.warnings.append(
                f"{employee.name} has requested {record_off.get('kind', 'time off')} covering this "
                "day; it is not yet decided."
            )

    required_certs = [c for c in (project.get("required_certifications") or []) if c]
    missing = employee.missing_certifications(required_certs, work_date)
    if missing:
        check.exceptions.append(
            f"{employee.name} is missing or expired on: {', '.join(sorted(missing))}. "
            "Assigning an employee without required certification requires management approval."
        )

    limit = float(employee.max_hours_per_week or OVERTIME_THRESHOLD_HOURS_PER_WEEK)
    threshold = min(limit, OVERTIME_THRESHOLD_HOURS_PER_WEEK)
    if projected_week > threshold and projected_week <= MAX_HOURS_PER_WEEK:
        check.exceptions.append(
            f"This shift puts {employee.name} at {projected_week:.1f}h for the week, past the "
            f"{threshold:.0f}h overtime threshold. Overtime beyond established limits requires "
            "management approval."
        )

    if status == ProjectStatus.TENTATIVE.value:
        check.warnings.append(
            "Project is tentative. Any crew committed here is provisional and must not be "
            "described to the employee or the client as confirmed."
        )

    # --- warnings ---------------------------------------------------------

    for expiring in employee.expiring_certifications(work_date):
        if not expiring["expired"] and expiring["certification"] in required_certs:
            check.warnings.append(
                f"{expiring['certification']} expires in {expiring['days_remaining']} days "
                f"({expiring['expires_on']}) — renew before it blocks this crew."
            )

    for skill in (project.get("required_skills") or []):
        level = employee.skill(str(skill))
        if level == int(SkillLevel.NONE):
            check.warnings.append(f"{employee.name} has no assessed capability in {skill}.")
        elif level <= int(SkillLevel.LEARNING):
            check.warnings.append(
                f"{employee.name} is still learning {skill} and should be paired with someone "
                "who can supervise."
            )

    access_hours = _access_hours(project)
    if access_hours is not None and hours > access_hours:
        check.warnings.append(
            f"Site access is {project.get('site_access_start')}–{project.get('site_access_end')} "
            f"({access_hours:.1f}h); a {hours:.1f}h shift will not fit inside it."
        )

    previous = ws.ops.assignments_on(work_date - timedelta(days=1), employee_id=employee_id)
    prior_projects = {str(a.get("project_id", "")) for a in previous}
    if prior_projects and project_id not in prior_projects:
        check.warnings.append(
            f"{employee.name} was on a different site yesterday — expect remobilization time "
            "and check travel before assuming a full production day."
        )

    return check


def _access_hours(project: dict[str, Any]) -> float | None:
    start, end = str(project.get("site_access_start", "")), str(project.get("site_access_end", ""))
    if ":" not in start or ":" not in end:
        return None
    try:
        first = int(start[:2]) * 60 + int(start[3:5])
        last = int(end[:2]) * 60 + int(end[3:5])
    except ValueError:
        return None
    return round((last - first) / 60.0, 2) if last > first else None


# --- availability ----------------------------------------------------------


def availability(ws: Workspace, day: date) -> dict[str, Any]:
    """Who is free on a day, who is not, and why — the input to every plan."""
    free: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []

    for record in ws.ops.list_employees(active_only=True):
        employee_id = str(record.get("id", ""))
        reasons: list[str] = []

        for off in ws.ops.time_off_for(employee_id, approved_only=True):
            start = str(off.get("start_date", ""))
            end = str(off.get("end_date", "") or start)
            if start and start <= day.isoformat() <= end:
                reasons.append(f"approved {off.get('kind', 'time off')} to {end}")

        booked = ws.ops.assignments_on(day, employee_id=employee_id)
        booked_hours = sum(float(a.get("hours", 0) or 0) for a in booked)
        if booked:
            names = ", ".join(
                str((ws.ops.get_project(str(a.get("project_id", ""))) or {}).get("name", "?"))
                for a in booked
            )
            reasons.append(f"already on {names} ({booked_hours:.1f}h)")

        week_hours = ws.ops.scheduled_hours_in_week(employee_id, day)
        entry = {
            "employee_id": employee_id,
            "name": record.get("name", ""),
            "crew_role": record.get("crew_role", ""),
            "can_lead": bool(record.get("can_lead", False)),
            "week_hours_committed": week_hours,
            "hours_free_today": round(max(MAX_HOURS_PER_DAY - booked_hours, 0.0), 2),
        }
        if reasons:
            unavailable.append({**entry, "reasons": reasons})
        else:
            free.append(entry)

    return {
        "date": day.isoformat(),
        "is_weekend": day.weekday() >= 5,
        "available": sorted(free, key=lambda e: str(e["name"])),
        "unavailable": sorted(unavailable, key=lambda e: str(e["name"])),
        "available_count": len(free),
        "roster_size": len(free) + len(unavailable),
    }


def to_employee(record: dict[str, Any]) -> Employee:
    """Rehydrate a stored employee so the domain logic can reason about it."""
    try:
        crew_role = CrewRole(str(record.get("crew_role", "painter")))
    except ValueError:
        crew_role = CrewRole.PAINTER

    return Employee(
        name=str(record.get("name", "")),
        crew_role=crew_role,
        phone=str(record.get("phone", "")),
        email=str(record.get("email", "")),
        skills={str(k): int(v or 0) for k, v in (record.get("skills") or {}).items()},
        certifications={str(k): str(v or "") for k, v in (record.get("certifications") or {}).items()},
        productivity_factor=float(record.get("productivity_factor", 1.0) or 1.0),
        reliability=float(record.get("reliability", 1.0) or 1.0),
        can_lead=bool(record.get("can_lead", False)),
        works_alone=bool(record.get("works_alone", True)),
        home_base=str(record.get("home_base", "")),
        max_hours_per_week=float(record.get("max_hours_per_week", OVERTIME_THRESHOLD_HOURS_PER_WEEK) or OVERTIME_THRESHOLD_HOURS_PER_WEEK),
        active=bool(record.get("active", True)),
        development_notes=str(record.get("development_notes", "")),
        notes=str(record.get("notes", "")),
        id=str(record.get("id", "")),
    )


# --- crew proposal ---------------------------------------------------------


def propose_crew(
    ws: Workspace,
    project_id: str,
    work_date: date,
    crew_size: int = 0,
) -> dict[str, Any]:
    """Recommend a crew for one project on one day, from people actually free."""
    project = ws.ops.get_project(project_id)
    if project is None:
        return {"error": f"no project with id {project_id}"}

    free = availability(ws, work_date)
    candidates: list[tuple[Employee, int]] = []
    for entry in free["available"]:
        record = ws.ops.get_employee(str(entry["employee_id"]))
        if record is None:
            continue
        candidates.append(
            (to_employee(record), ws.ops.days_worked_on_project(str(entry["employee_id"]), project_id))
        )

    proposal = assemble_crew(project, candidates, crew_size=crew_size, on=work_date)
    result = proposal.to_dict()
    result.update(
        {
            "project_name": project.get("name", ""),
            "work_date": work_date.isoformat(),
            "requires_lead": requires_lead(project),
            "unavailable": free["unavailable"],
            "weather": exterior_weather(ws, project, work_date),
            "status": "proposal_only",
            "instruction": (
                "This is a recommendation, not a schedule. Nothing is booked until "
                "assign_crew succeeds, and nothing is committed to a person or a client "
                "until the shift is confirmed."
            ),
        }
    )
    return result


def exterior_weather(ws: Workspace, project: dict[str, Any], start: date, days: int = 3) -> dict[str, Any]:
    """Weather risk for exterior scope — or an honest UNKNOWN.

    A simulated forecast must never drive a real decision about whether
    coatings can be applied, so an unconfigured weather service returns
    UNKNOWN rather than a confident-looking assessment.
    """
    if not project.get("is_exterior"):
        return {"applicable": False, "reason": "interior scope — ambient forecast does not gate it"}
    city = str(project.get("city", "") or "")
    if not city:
        return {"applicable": True, "risk": "unknown", "reason": "project has no city set"}
    if not ws.weather.live:
        return {
            "applicable": True,
            "risk": "unknown",
            "_mocked": True,
            "reason": (
                "Weather service is not connected. Exterior conditions are UNKNOWN — do not "
                "present a forecast as fact or promise exterior production on these dates."
            ),
        }
    return {"applicable": True, **ws.weather.paint_window_risk(city, start, days)}


# --- the seven-day plan ----------------------------------------------------


def rank_projects(ws: Workspace, on: date | None = None) -> list[dict[str, Any]]:
    """Projects that need crew, in the spec's priority order."""
    today = on or date.today()
    ranked: list[dict[str, Any]] = []

    for project in ws.ops.list_projects():
        if str(project.get("status", "")) not in SCHEDULABLE_STATUSES:
            continue
        remaining = float(project.get("estimated_hours", 0) or 0) - float(project.get("actual_hours", 0) or 0)
        entry = dict(project)
        entry["hours_remaining"] = round(max(remaining, 0.0), 2)
        entry["deadline_pressure_hours_per_day"] = deadline_pressure(entry, today)
        entry["days_to_deadline"] = _days_to(project.get("deadline"), today)
        ranked.append(entry)

    return sorted(
        ranked,
        key=lambda p: (
            priority_of(p),
            -float(p["deadline_pressure_hours_per_day"] if p["deadline_pressure_hours_per_day"] != float("inf") else 1e9),
            str(p.get("deadline", "9999-12-31")),
        ),
    )


def _days_to(deadline: Any, today: date) -> int | None:
    if not deadline:
        return None
    try:
        return (date.fromisoformat(str(deadline)) - today).days
    except ValueError:
        return None


def plan_week(ws: Workspace, start: date, days: int = 7, include_weekends: bool = False) -> dict[str, Any]:
    """Build a provisional crew schedule across the next working days.

    Nothing is written. The plan is explicitly labelled provisional because
    an unwritten plan that reads like a commitment is exactly the failure the
    spec calls out — a tentative schedule presented as confirmed.
    """
    calendar_days = working_days(start, days, include_weekends=include_weekends)
    projects = rank_projects(ws, start)

    # Track what this plan has already spent, so one pass cannot book the
    # same person twice or promise hours the week cannot hold. Overtime is a
    # per-week limit, so hours are keyed by (person, Monday of that week) —
    # a planning window spanning a week boundary must not carry Monday's
    # ceiling into the next week.
    committed_days: dict[str, set[str]] = {}
    committed_week_hours: dict[tuple[str, str], float] = {}

    def week_hours(employee_id: str, day: date) -> float:
        key = (employee_id, week_start(day).isoformat())
        if key not in committed_week_hours:
            committed_week_hours[key] = ws.ops.scheduled_hours_in_week(employee_id, day)
        return committed_week_hours[key]

    plan: list[dict[str, Any]] = []
    unstaffed: list[dict[str, Any]] = []

    for day in calendar_days:
        free = availability(ws, day)
        free_ids = {str(e["employee_id"]) for e in free["available"]}

        for project in projects:
            project_id = str(project["id"])
            if not _project_wants_crew(ws, project, day):
                continue

            phases = _ready_phases(ws, project_id)
            blocked = [p for p in phases if not p.get("schedulable")]
            ready = [p for p in phases if p.get("schedulable")]
            if phases and not ready:
                unstaffed.append(
                    {
                        "date": day.isoformat(),
                        "project": project.get("name", ""),
                        "project_id": project_id,
                        "reason": "every remaining phase is blocked by an unfinished predecessor",
                        "blocked_by": [b.get("blocked_by") for b in blocked[:3]],
                    }
                )
                continue

            candidates: list[tuple[Employee, int]] = []
            for employee_id in sorted(free_ids):
                if day.isoformat() in committed_days.get(employee_id, set()):
                    continue
                if week_hours(employee_id, day) + STANDARD_HOURS_PER_DAY > OVERTIME_THRESHOLD_HOURS_PER_WEEK:
                    continue
                record = ws.ops.get_employee(employee_id)
                if record is None:
                    continue
                candidates.append(
                    (to_employee(record), ws.ops.days_worked_on_project(employee_id, project_id))
                )

            if not candidates:
                unstaffed.append(
                    {
                        "date": day.isoformat(),
                        "project": project.get("name", ""),
                        "project_id": project_id,
                        "reason": "nobody available within standard hours",
                        "priority": int(project.get("priority", 4) or 4),
                    }
                )
                continue

            proposal = assemble_crew(project, candidates, on=day)
            if not proposal.crew:
                unstaffed.append(
                    {
                        "date": day.isoformat(),
                        "project": project.get("name", ""),
                        "project_id": project_id,
                        "reason": "no eligible crew could be formed",
                        "warnings": proposal.warnings,
                    }
                )
                continue

            phase = ready[0]["phase"] if ready else ""
            for member in proposal.crew:
                employee_id = str(member["employee_id"])
                committed_days.setdefault(employee_id, set()).add(day.isoformat())
                committed_week_hours[(employee_id, week_start(day).isoformat())] = (
                    week_hours(employee_id, day) + STANDARD_HOURS_PER_DAY
                )

            plan.append(
                {
                    "date": day.isoformat(),
                    "project_id": project_id,
                    "project": project.get("name", ""),
                    "address": project.get("address", ""),
                    "phase": phase,
                    "crew": [
                        {
                            "employee_id": m["employee_id"],
                            "name": m["employee_name"],
                            "fit_score": m["score"],
                            "is_lead": m["employee_id"] == proposal.lead_employee_id,
                        }
                        for m in proposal.crew
                    ],
                    "lead_employee_id": proposal.lead_employee_id,
                    "hours_each": STANDARD_HOURS_PER_DAY,
                    "labour_hours": round(len(proposal.crew) * STANDARD_HOURS_PER_DAY, 2),
                    "warnings": proposal.warnings,
                    "confidence": proposal.confidence,
                    "weather": exterior_weather(ws, project, day, days=1),
                }
            )

    return {
        "window": {
            "start": start.isoformat(),
            "days": days,
            "working_days": [d.isoformat() for d in calendar_days],
        },
        "status": "provisional",
        "shifts": plan,
        "total_shifts": sum(len(s["crew"]) for s in plan),
        "planned_labour_hours": round(sum(float(s["labour_hours"]) for s in plan), 2),
        "unstaffed": unstaffed,
        "projects_considered": [
            {
                "id": p["id"],
                "name": p.get("name", ""),
                "priority": int(p.get("priority", 4) or 4),
                "status": p.get("status", ""),
                "hours_remaining": p.get("hours_remaining", 0),
                "days_to_deadline": p.get("days_to_deadline"),
                "deadline_pressure_hours_per_day": p.get("deadline_pressure_hours_per_day"),
            }
            for p in projects
        ],
        "instruction": (
            "PROVISIONAL. No shift here exists until it is written with assign_crew, and no "
            "shift is a commitment to anyone until it is confirmed. Describe it as a proposed "
            "schedule when you report it."
        ),
    }


def _project_wants_crew(ws: Workspace, project: dict[str, Any], day: date) -> bool:
    if float(project.get("hours_remaining", 0) or 0) <= 0:
        return False
    start = str(project.get("start_date", "") or "")
    if start and day.isoformat() < start:
        return False
    deadline = str(project.get("deadline", "") or "")
    if deadline and day.isoformat() > deadline:
        return False
    if str(project.get("status", "")) == ProjectStatus.ON_HOLD.value:
        return False
    return True


def _ready_phases(ws: Workspace, project_id: str) -> list[dict[str, Any]]:
    phases = ws.ops.phases_for(project_id)
    return next_schedulable_phases(phases) if phases else []


# --- conflicts and monitoring ----------------------------------------------


@dataclass
class Alert:
    """One monitoring finding, in the shape the spec requires."""

    severity: str            # critical | high | medium | low
    what: str
    why_it_matters: str
    affected: list[str] = field(default_factory=list)
    expected_impact: str = ""
    recommended_action: str = ""
    approval_required: str = "none"
    decide_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "what_happened": self.what,
            "why_it_matters": self.why_it_matters,
            "affected": self.affected,
            "expected_impact": self.expected_impact,
            "recommended_action": self.recommended_action,
            "approval_required": self.approval_required,
            "decide_by": self.decide_by,
        }


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def scheduling_risks(ws: Workspace, on: date | None = None, horizon_days: int = 14) -> dict[str, Any]:
    """The prioritized scheduling-risk report.

    Every check here is computed from stored records. Where a data source is
    not connected the report says so rather than reporting an empty result as
    a clean bill of health.
    """
    today = on or date.today()
    horizon = today + timedelta(days=horizon_days)
    alerts: list[Alert] = []

    projects = {str(p["id"]): p for p in ws.ops.list_projects()}
    employees = {str(e["id"]): e for e in ws.ops.list_employees(active_only=False)}
    upcoming = ws.ops.assignments_between(today, horizon)

    # 1. Existing double-bookings. Should be impossible through the tools;
    #    checked anyway, because records can arrive from elsewhere.
    by_person_day: dict[tuple[str, str], set[str]] = {}
    for record in upcoming:
        key = (str(record.get("employee_id", "")), str(record.get("work_date", "")))
        by_person_day.setdefault(key, set()).add(str(record.get("project_id", "")))
    for (employee_id, day), project_ids in sorted(by_person_day.items()):
        if len(project_ids) > 1:
            alerts.append(
                Alert(
                    severity="critical",
                    what=(
                        f"{_name(employees, employee_id)} is booked on "
                        f"{len(project_ids)} projects on {day}."
                    ),
                    why_it_matters="One person cannot be on two sites. Both projects are planning on labour that does not exist.",
                    affected=[_project_name(projects, p) for p in sorted(project_ids)],
                    expected_impact="At least one project will be short-crewed on the day, without warning.",
                    recommended_action="Cancel the lower-priority shift and re-staff it from the available roster.",
                    decide_by=day,
                )
            )

    # 2. Assignments landing on approved time off.
    for record in upcoming:
        employee_id = str(record.get("employee_id", ""))
        work_date = str(record.get("work_date", ""))
        for off in ws.ops.time_off_for(employee_id, approved_only=True):
            start = str(off.get("start_date", ""))
            end = str(off.get("end_date", "") or start)
            if start and start <= work_date <= end and record.get("approval_id", "") == "":
                alerts.append(
                    Alert(
                        severity="critical",
                        what=f"{_name(employees, employee_id)} is scheduled on {work_date} during approved {off.get('kind', 'time off')}.",
                        why_it_matters="Approved time off is a commitment to the employee. Overriding it without approval is a policy breach.",
                        affected=[_project_name(projects, str(record.get("project_id", "")))],
                        expected_impact="Likely no-show, an unstaffed day, and a damaged commitment to the employee.",
                        recommended_action="Cancel the shift and re-staff, or escalate for a management exception.",
                        approval_required="management",
                        decide_by=work_date,
                    )
                )

    # 3. Projects starting soon with no confirmed crew.
    for project in projects.values():
        if str(project.get("status", "")) not in SCHEDULABLE_STATUSES:
            continue
        start = str(project.get("start_date", "") or "")
        if not start or not (today.isoformat() <= start <= (today + timedelta(days=UNSTAFFED_WARNING_DAYS)).isoformat()):
            continue
        confirmed = [
            a for a in ws.ops.assignments_for_project(str(project["id"]))
            if a.get("status") == AssignmentStatus.CONFIRMED.value
        ]
        if confirmed:
            continue
        days_out = _days_to(start, today) or 0
        alerts.append(
            Alert(
                severity="critical" if days_out <= 2 else "high",
                what=f"{project.get('name', '')} starts on {start} with no confirmed crew.",
                why_it_matters="An unstaffed start date is a missed commitment to the client and often blocks another trade.",
                affected=[str(project.get("name", "")), str(project.get("client_name", ""))],
                expected_impact=f"Start slips; {project.get('deadline') or 'the deadline'} comes under pressure.",
                recommended_action="Propose and confirm a crew now, or notify the client of a revised start date.",
                decide_by=start,
            )
        )

    # 4. Deadline pressure the roster cannot absorb.
    for project in projects.values():
        if str(project.get("status", "")) not in SCHEDULABLE_STATUSES:
            continue
        remaining = float(project.get("estimated_hours", 0) or 0) - float(project.get("actual_hours", 0) or 0)
        entry = dict(project)
        entry["hours_remaining"] = max(remaining, 0.0)
        pressure = deadline_pressure(entry, today)
        if pressure <= 0:
            continue
        crew_target = max(int(project.get("crew_size_target", 2) or 2), 1)
        capacity = crew_target * STANDARD_HOURS_PER_DAY
        if pressure > capacity:
            alerts.append(
                Alert(
                    severity="high" if pressure < capacity * 2 else "critical",
                    what=(
                        f"{project.get('name', '')} needs {pressure:.1f} labour hours per working day "
                        f"to hit {project.get('deadline', 'its deadline')}, against a planned crew "
                        f"capacity of {capacity:.0f}h/day."
                    ),
                    why_it_matters="The deadline is not achievable at the planned crew size.",
                    affected=[str(project.get("name", ""))],
                    expected_impact="Either the deadline moves, the crew grows, or overtime is authorized.",
                    recommended_action="Present the three options with cost and risk, and get a decision.",
                    approval_required="management",
                    decide_by=str(project.get("deadline", "")),
                )
            )

    # 5. Labour budget overruns.
    for project in projects.values():
        budget = float(project.get("labour_budget_hours", 0) or 0)
        actual = float(project.get("actual_hours", 0) or 0)
        if budget <= 0 or actual <= 0:
            continue
        used = actual / budget
        if used >= 1.0:
            severity, phrase = "high", f"is {used:.0%} of budget — already over"
        elif used >= BUDGET_WARNING_FRACTION:
            severity, phrase = "medium", f"is at {used:.0%} of budget"
        else:
            continue
        alerts.append(
            Alert(
                severity=severity,
                what=f"{project.get('name', '')} {phrase} ({actual:.0f}h of {budget:.0f}h).",
                why_it_matters="Labour is the margin on a painting contract; an overrun is a direct profit loss.",
                affected=[str(project.get("name", ""))],
                expected_impact="Projected overrun if production continues at the current rate.",
                recommended_action="Review remaining scope against remaining budget and re-forecast before adding hours.",
            )
        )

    # 6. Certifications expired or expiring.
    for record in employees.values():
        if not record.get("active", True):
            continue
        employee = to_employee(record)
        for expiring in employee.expiring_certifications(today):
            alerts.append(
                Alert(
                    severity="high" if expiring["expired"] else "medium",
                    what=(
                        f"{employee.name}'s {expiring['certification']} "
                        + ("expired on " if expiring["expired"] else "expires on ")
                        + expiring["expires_on"]
                    ),
                    why_it_matters=(
                        "An expired certification disqualifies the employee from the work it "
                        "gates, and exposes the company on site."
                        if expiring["expired"]
                        else "Once it lapses, the employee is disqualified from the work it gates "
                             "and crews built on them have to be rebuilt at short notice."
                    ),
                    affected=[employee.name],
                    expected_impact="Any project requiring that certification loses this person from the eligible pool.",
                    recommended_action="Book renewal and re-check crews on projects that require it.",
                    decide_by=expiring["expires_on"],
                )
            )

    # 7. Overtime already committed.
    for employee_id, record in employees.items():
        if not record.get("active", True):
            continue
        hours = ws.ops.scheduled_hours_in_week(employee_id, today)
        limit = float(record.get("max_hours_per_week", OVERTIME_THRESHOLD_HOURS_PER_WEEK) or OVERTIME_THRESHOLD_HOURS_PER_WEEK)
        if hours > limit:
            alerts.append(
                Alert(
                    severity="high" if hours > MAX_HOURS_PER_WEEK * 0.9 else "medium",
                    what=f"{record.get('name', '')} is scheduled {hours:.1f}h in the week of {week_start(today).isoformat()}, past their {limit:.0f}h limit.",
                    why_it_matters="Sustained overtime costs premium labour and degrades quality and safety.",
                    affected=[str(record.get("name", ""))],
                    expected_impact="Overtime cost and fatigue risk on every project they touch this week.",
                    recommended_action="Redistribute the excess to available crew or get overtime authorized.",
                    approval_required="management",
                )
            )

    # 8. Drafts that are about to become reality.
    cutoff = (today + timedelta(days=DRAFT_CONFIRMATION_LEAD_DAYS)).isoformat()
    stale_drafts = [
        a for a in upcoming
        if a.get("status") == AssignmentStatus.DRAFT.value and str(a.get("work_date", "")) <= cutoff
    ]
    if stale_drafts:
        alerts.append(
            Alert(
                severity="medium",
                what=f"{len(stale_drafts)} draft shift(s) fall within {DRAFT_CONFIRMATION_LEAD_DAYS} days and are still unconfirmed.",
                why_it_matters="Nobody has been told to show up. A draft is not a schedule, and the crew cannot read it.",
                affected=sorted({_project_name(projects, str(a.get("project_id", ""))) for a in stale_drafts}),
                expected_impact="Crews arrive at the wrong site, or not at all.",
                recommended_action="Confirm the shifts and send the crew notifications, or drop them from the plan.",
                decide_by=cutoff,
            )
        )

    # 9. Attendance — only assertable when the timeclock is connected.
    yesterday = today - timedelta(days=1)
    scheduled_yesterday = ws.ops.assignments_between(yesterday, yesterday)
    if scheduled_yesterday:
        if not ws.timeclock.live:
            alerts.append(
                Alert(
                    severity="low",
                    what="Attendance against yesterday's schedule cannot be verified: no timeclock is connected.",
                    why_it_matters="Late clock-ins, absences and unplanned overtime are invisible without it.",
                    affected=["attendance monitoring"],
                    expected_impact="Variance between planned and actual hours stays UNKNOWN, so estimates cannot improve.",
                    recommended_action="Connect the timeclock, or collect daily reports manually until it is.",
                )
            )
        else:
            logged = {str(e.get("employee_id", "")) for e in ws.ops.time_entries_on(yesterday)}
            missing = [
                _name(employees, str(a.get("employee_id", "")))
                for a in scheduled_yesterday
                if str(a.get("employee_id", "")) not in logged
            ]
            if missing:
                alerts.append(
                    Alert(
                        severity="high",
                        what=f"No clock-in recorded yesterday for: {', '.join(sorted(set(missing)))}.",
                        why_it_matters="An unrecorded shift is either an absence nobody caught or a payroll and progress record that is now wrong.",
                        affected=sorted(set(missing)),
                        expected_impact="Production on those projects is behind what the schedule assumes.",
                        recommended_action="Confirm with the team lead, then correct the record and re-forecast.",
                    )
                )

    # 10. Equipment booked twice.
    for booking in ws.ops.equipment_bookings():
        equipment_id = str(booking.get("equipment_id", ""))
        for other in ws.ops.equipment_bookings(equipment_id):
            if other.get("id") == booking.get("id"):
                continue
            if _ranges_overlap(booking, other) and str(booking.get("id", "")) < str(other.get("id", "")):
                alerts.append(
                    Alert(
                        severity="medium",
                        what=f"Equipment {equipment_id} is booked to two projects over the same dates.",
                        why_it_matters="A sprayer or lift in two places stops production on one of them.",
                        affected=[
                            _project_name(projects, str(booking.get("project_id", ""))),
                            _project_name(projects, str(other.get("project_id", ""))),
                        ],
                        expected_impact="Lost production day on the lower-priority project.",
                        recommended_action="Re-sequence one project or arrange a rental.",
                    )
                )

    # 11. Projects with no phase plan.
    unplanned = [
        p.get("name", "")
        for p in projects.values()
        if str(p.get("status", "")) in SCHEDULABLE_STATUSES and not ws.ops.phases_for(str(p["id"]))
    ]
    if unplanned:
        alerts.append(
            Alert(
                severity="low",
                what=f"{len(unplanned)} project(s) have no phase plan: {', '.join(sorted(unplanned)[:5])}.",
                why_it_matters="Without phases there is no dependency or cure-time check, so coatings can be scheduled out of sequence.",
                affected=sorted(unplanned),
                expected_impact="Sequencing errors, rework, and unrealistic durations.",
                recommended_action="Run plan_project_phases on each before scheduling crew.",
            )
        )

    ranked = sorted(alerts, key=lambda a: (SEVERITY_ORDER.get(a.severity, 9), a.what))
    counts: dict[str, int] = {}
    for alert in ranked:
        counts[alert.severity] = counts.get(alert.severity, 0) + 1

    return {
        "generated_for": today.isoformat(),
        "horizon_days": horizon_days,
        "counts": counts,
        "alerts": [a.to_dict() for a in ranked],
        "data_sources": {
            "operations_records": "live" if ws.ops.live else "local store (authoritative)",
            "timeclock": "live" if ws.timeclock.live else "not connected — attendance is UNKNOWN",
            "weather": "live" if ws.weather.live else "not connected — exterior risk is UNKNOWN",
        },
    }


def _ranges_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    a_start, a_end = str(a.get("start_date", "")), str(a.get("end_date", "") or a.get("start_date", ""))
    b_start, b_end = str(b.get("start_date", "")), str(b.get("end_date", "") or b.get("start_date", ""))
    if not (a_start and b_start):
        return False
    return a_start <= b_end and b_start <= a_end


def _name(employees: dict[str, dict[str, Any]], employee_id: str) -> str:
    return str(employees.get(employee_id, {}).get("name", employee_id))


def _project_name(projects: dict[str, dict[str, Any]], project_id: str) -> str:
    return str(projects.get(project_id, {}).get("name", project_id))


# --- learning --------------------------------------------------------------


def variance_report(ws: Workspace, project_id: str = "", on: date | None = None) -> dict[str, Any]:
    """Planned against actual — the evidence the ARE loop closes on.

    Estimate accuracy cannot improve without this comparison, and the
    comparison is worthless if actuals are guessed. Where the timeclock is
    not connected, actual hours are reported as unverified.
    """
    today = on or date.today()
    projects = [ws.ops.get_project(project_id)] if project_id else ws.ops.list_projects()
    projects = [p for p in projects if p]

    rows: list[dict[str, Any]] = []
    for project in projects:
        pid = str(project["id"])
        estimated = float(project.get("estimated_hours", 0) or 0)
        actual = float(project.get("actual_hours", 0) or 0)
        scheduled = sum(
            float(a.get("hours", 0) or 0) for a in ws.ops.assignments_for_project(pid)
            if str(a.get("status", "")) in {AssignmentStatus.CONFIRMED.value, AssignmentStatus.COMPLETED.value}
        )
        clocked = sum(
            float(e.get("hours", 0) or 0)
            for e in ws.ops.time_entries_between(today - timedelta(days=365), today, project_id=pid)
        )
        phases = ws.ops.phases_for(pid)
        complete = [p for p in phases if str(p.get("status", "")) == PhaseStatus.COMPLETE.value]

        rows.append(
            {
                "project_id": pid,
                "project": project.get("name", ""),
                "status": project.get("status", ""),
                "estimated_hours": round(estimated, 2),
                "scheduled_hours": round(scheduled, 2),
                "recorded_actual_hours": round(actual, 2),
                "clocked_hours": round(clocked, 2),
                "hours_variance": round(actual - estimated, 2) if estimated and actual else None,
                "estimate_accuracy": round(estimated / actual, 3) if actual > 0 and estimated > 0 else None,
                "labour_budget_hours": float(project.get("labour_budget_hours", 0) or 0),
                "phases_complete": f"{len(complete)}/{len(phases)}" if phases else "no phase plan",
                "deadline": project.get("deadline", ""),
                "days_to_deadline": _days_to(project.get("deadline"), today),
            }
        )

    measurable = [r for r in rows if r["estimate_accuracy"] is not None]
    return {
        "generated_for": today.isoformat(),
        "projects": rows,
        "projects_with_measurable_variance": len(measurable),
        "mean_estimate_accuracy": (
            round(sum(float(r["estimate_accuracy"]) for r in measurable) / len(measurable), 3)
            if measurable
            else None
        ),
        "actuals_verified": ws.timeclock.live,
        "caveat": (
            "Actual hours are self-reported; no timeclock is connected, so treat variance as "
            "INFERENCE rather than KNOWN FACT."
            if not ws.timeclock.live
            else "Actual hours are drawn from recorded clock entries."
        ),
    }
