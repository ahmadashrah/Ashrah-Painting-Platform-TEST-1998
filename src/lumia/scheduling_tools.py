"""The tool surface the Scheduling Agent acts through.

Registered into the same `Toolbox` as the growth tools, so scheduling
actions pass the same autonomy gate. Two design rules carry most of the
weight here:

1. **The clean path and the exception path are different tools.**
   `assign_crew` refuses anything needing a management decision and names
   the decision. `request_scheduling_exception` is that decision, and it is
   Level 3, so it cannot execute without a human. The agent cannot talk its
   way past a constraint, because the constraint is not in the prompt.

2. **Messages are composed by the tool, not by the model.**
   A crew briefing that omits the address or the lead is a real operational
   failure, and a client note that leaks labour cost is a real commercial
   one. Both messages are built here from structured fields, so neither
   omission nor leakage is available as an option.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .domain.crewing import WEIGHTS, requires_lead
from .domain.projects import (
    Phase,
    PhaseStatus,
    Project,
    ProjectPriority,
    ProjectStatus,
    phase_plan,
)
from .domain.workforce import (
    CERTIFICATIONS,
    SKILLS,
    STANDARD_HOURS_PER_DAY,
    Assignment,
    AssignmentStatus,
    CrewRole,
    Employee,
    EquipmentBooking,
    TimeEntry,
    TimeOff,
    TimeOffKind,
    TimeOffStatus,
)
from .scheduling import (
    availability,
    check_assignment,
    exterior_weather,
    plan_week,
    propose_crew,
    rank_projects,
    scheduling_risks,
    to_employee,
    variance_report,
)
from .workspace import Workspace

def _enum(enum_cls: Any, value: Any, fallback: Any) -> Any:
    try:
        return enum_cls(value)
    except (ValueError, KeyError):
        return fallback


def _parse_date(value: str, field: str = "date") -> date | str:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return f"{field} must be an ISO date, e.g. 2026-08-17"


class SchedulingTools:
    """Scheduling capabilities, bound to a workspace and registered into a Toolbox."""

    def __init__(self, workspace: Workspace) -> None:
        self.ws = workspace

    # --- projects (read) --------------------------------------------------

    def _list_projects(self, status: str = "", limit: int = 25) -> dict[str, Any]:
        ranked = rank_projects(self.ws)
        if status:
            ranked = [p for p in ranked if str(p.get("status", "")) == status]
        return {
            "count": len(ranked),
            "priority_order": "lower priority number is scheduled first",
            "projects": [
                {
                    "id": p["id"],
                    "name": p.get("name", ""),
                    "status": p.get("status", ""),
                    "priority": p.get("priority"),
                    "start_date": p.get("start_date", ""),
                    "deadline": p.get("deadline", ""),
                    "days_to_deadline": p.get("days_to_deadline"),
                    "hours_remaining": p.get("hours_remaining"),
                    "deadline_pressure_hours_per_day": p.get("deadline_pressure_hours_per_day"),
                    "crew_size_target": p.get("crew_size_target"),
                    "is_exterior": p.get("is_exterior", False),
                }
                for p in ranked[: max(limit, 1)]
            ],
        }

    def _find_projects(self, term: str) -> dict[str, Any]:
        matches = self.ws.ops.find_projects(term)
        return {
            "query": term,
            "count": len(matches),
            "projects": [
                {"id": p["id"], "name": p.get("name", ""), "status": p.get("status", ""),
                 "address": p.get("address", ""), "deadline": p.get("deadline", "")}
                for p in matches[:20]
            ],
        }

    def _get_project(self, project_id: str) -> dict[str, Any]:
        project = self.ws.ops.get_project(project_id)
        if project is None:
            return {"error": f"no project with id {project_id}"}
        assignments = self.ws.ops.assignments_for_project(project_id)
        return {
            "project": project,
            "phases": self.ws.ops.phases_for(project_id),
            "assignments": assignments,
            "confirmed_shifts": sum(
                1 for a in assignments if a.get("status") == AssignmentStatus.CONFIRMED.value
            ),
            "draft_shifts": sum(
                1 for a in assignments if a.get("status") == AssignmentStatus.DRAFT.value
            ),
            "requires_lead_on_site": requires_lead(project),
            "linked_account_id": project.get("account_id", ""),
        }

    def _project_schedule(self, project_id: str, start_date: str = "", days: int = 14) -> dict[str, Any]:
        project = self.ws.ops.get_project(project_id)
        if project is None:
            return {"error": f"no project with id {project_id}"}
        start = _parse_date(start_date) if start_date else date.today()
        if isinstance(start, str):
            return {"error": start}
        end = start + timedelta(days=max(days, 1))
        shifts = self.ws.ops.assignments_between(start, end, project_id=project_id)

        by_day: dict[str, list[dict[str, Any]]] = {}
        for shift in shifts:
            person = self.ws.ops.get_employee(str(shift.get("employee_id", ""))) or {}
            by_day.setdefault(str(shift.get("work_date", "")), []).append(
                {
                    "assignment_id": shift.get("id"),
                    "employee": person.get("name", shift.get("employee_id")),
                    "phase": shift.get("phase", ""),
                    "hours": shift.get("hours"),
                    "is_lead": shift.get("is_lead", False),
                    "status": shift.get("status"),
                }
            )
        return {
            "project": project.get("name", ""),
            "project_id": project_id,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "shifts_by_day": dict(sorted(by_day.items())),
            "total_shifts": len(shifts),
        }

    # --- roster (read) ----------------------------------------------------

    def _list_crew(self, available_on: str = "", skill: str = "") -> dict[str, Any]:
        if available_on:
            day = _parse_date(available_on, "available_on")
            if isinstance(day, str):
                return {"error": day}
            return availability(self.ws, day)

        roster = []
        for record in self.ws.ops.list_employees(active_only=True):
            if skill and int((record.get("skills") or {}).get(skill, 0) or 0) <= 0:
                continue
            roster.append(
                {
                    "id": record["id"],
                    "name": record.get("name", ""),
                    "crew_role": record.get("crew_role", ""),
                    "can_lead": record.get("can_lead", False),
                    "skills": record.get("skills", {}),
                    "certifications": sorted((record.get("certifications") or {}).keys()),
                }
            )
        return {
            "count": len(roster),
            "filtered_by_skill": skill,
            "crew": roster,
            "privacy_note": (
                "Roster detail is internal. Never send skills, assessments, reliability "
                "figures or development notes to a client, a GC, or another employee."
            ),
        }

    def _get_crew_member(self, employee_id: str) -> dict[str, Any]:
        record = self.ws.ops.get_employee(employee_id)
        if record is None:
            return {"error": f"no employee with id {employee_id}"}
        employee = to_employee(record)
        today = date.today()
        return {
            "employee": record,
            "expiring_certifications": employee.expiring_certifications(today),
            "approved_time_off": self.ws.ops.time_off_for(employee_id, approved_only=True),
            "requested_time_off": [
                t for t in self.ws.ops.time_off_for(employee_id)
                if t.get("status") == TimeOffStatus.REQUESTED.value
            ],
            "hours_this_week": self.ws.ops.scheduled_hours_in_week(employee_id, today),
            "upcoming_shifts": self.ws.ops.assignments_between(today, today + timedelta(days=14), employee_id=employee_id),
            "privacy_note": "Internal only. Performance and development detail is not for external recipients.",
        }

    def _crew_availability(self, work_date: str) -> dict[str, Any]:
        day = _parse_date(work_date, "work_date")
        if isinstance(day, str):
            return {"error": day}
        return availability(self.ws, day)

    # --- analysis (read) --------------------------------------------------

    def _check_shift(
        self, project_id: str, employee_id: str, work_date: str, hours: float = STANDARD_HOURS_PER_DAY
    ) -> dict[str, Any]:
        day = _parse_date(work_date, "work_date")
        if isinstance(day, str):
            return {"error": day}
        check = check_assignment(self.ws, project_id, employee_id, day, float(hours))
        result = check.to_dict()
        result["guidance"] = (
            "Blocks cannot be approved away — change the plan. Exceptions are management "
            "decisions: use request_scheduling_exception with the justification."
        )
        return result

    def _propose_crew(self, project_id: str, work_date: str, crew_size: int = 0) -> dict[str, Any]:
        day = _parse_date(work_date, "work_date")
        if isinstance(day, str):
            return {"error": day}
        result = propose_crew(self.ws, project_id, day, crew_size=int(crew_size or 0))
        result["scoring_weights"] = WEIGHTS
        return result

    def _plan_seven_day_schedule(self, start_date: str = "", days: int = 7, include_weekends: bool = False) -> dict[str, Any]:
        start = _parse_date(start_date, "start_date") if start_date else date.today()
        if isinstance(start, str):
            return {"error": start}
        return plan_week(self.ws, start, days=int(days or 7), include_weekends=bool(include_weekends))

    def _risk_report(self, horizon_days: int = 14) -> dict[str, Any]:
        return scheduling_risks(self.ws, horizon_days=int(horizon_days or 14))

    def _variance_report(self, project_id: str = "") -> dict[str, Any]:
        return variance_report(self.ws, project_id=project_id)

    def _weather_check(self, project_id: str, start_date: str = "", days: int = 3) -> dict[str, Any]:
        project = self.ws.ops.get_project(project_id)
        if project is None:
            return {"error": f"no project with id {project_id}"}
        start = _parse_date(start_date, "start_date") if start_date else date.today()
        if isinstance(start, str):
            return {"error": start}
        return exterior_weather(self.ws, project, start, days=int(days or 3))

    # --- record keeping (Level 2) -----------------------------------------

    def _upsert_project(
        self,
        name: str,
        project_id: str = "",
        address: str = "",
        city: str = "",
        client_name: str = "",
        client_email: str = "",
        account_id: str = "",
        status: str = "",
        priority: int = 0,
        start_date: str = "",
        deadline: str = "",
        estimated_hours: float = 0.0,
        labour_budget_hours: float = 0.0,
        crew_size_target: int = 0,
        required_skills: list[str] | None = None,
        required_certifications: list[str] | None = None,
        includes_specialty_coating: bool = False,
        is_exterior: bool = False,
        is_occupied: bool = False,
        site_access_start: str = "",
        site_access_end: str = "",
        access_notes: str = "",
        blocking_trade: str = "",
        scope_notes: str = "",
    ) -> dict[str, Any]:
        existing = self.ws.ops.get_project(project_id) if project_id else None
        project = Project(
            name=name,
            address=address,
            city=city,
            client_name=client_name,
            client_email=client_email,
            account_id=account_id,
            status=_enum(ProjectStatus, status, ProjectStatus.TENTATIVE),
            priority=_enum(ProjectPriority, int(priority or 0), ProjectPriority.CLIENT_COMMITMENT),
            start_date=start_date,
            deadline=deadline,
            estimated_hours=float(estimated_hours or 0),
            labour_budget_hours=float(labour_budget_hours or estimated_hours or 0),
            crew_size_target=int(crew_size_target or 2),
            required_skills=[s for s in (required_skills or []) if s],
            required_certifications=[c for c in (required_certifications or []) if c],
            includes_specialty_coating=bool(includes_specialty_coating),
            is_exterior=bool(is_exterior),
            is_occupied=bool(is_occupied),
            site_access_start=site_access_start or "07:00",
            site_access_end=site_access_end or "17:00",
            access_notes=access_notes,
            blocking_trade=blocking_trade,
            scope_notes=scope_notes,
            id=project_id or Project(name=name).id,
        )
        record = self.ws.ops.upsert_project(project.to_dict())
        return {
            "project": record,
            "created": existing is None,
            "next_step": (
                "Run plan_project_phases so dependencies and cure windows are enforced "
                "before any crew is scheduled."
                if not self.ws.ops.phases_for(record["id"])
                else "Phase plan already exists."
            ),
        }

    def _plan_project_phases(self, project_id: str) -> dict[str, Any]:
        project = self.ws.ops.get_project(project_id)
        if project is None:
            return {"error": f"no project with id {project_id}"}
        if float(project.get("estimated_hours", 0) or 0) <= 0:
            return {
                "error": "project has no estimated_hours; a phase plan built on zero hours is "
                         "a fiction. Set the labour estimate first."
            }
        existing = self.ws.ops.phases_for(project_id)
        if existing:
            return {
                "project_id": project_id,
                "phases": existing,
                "note": "Phase plan already exists; progress was not overwritten.",
            }
        phases = [p.to_dict() for p in phase_plan(project)]
        self.ws.ops.put_phases(phases)
        return {
            "project_id": project_id,
            "project": project.get("name", ""),
            "phases": phases,
            "total_estimated_hours": round(sum(float(p["estimated_hours"]) for p in phases), 2),
            "note": (
                "Cure windows are enforced between coating phases. A phase whose predecessor "
                "is incomplete will report as blocked rather than being scheduled."
            ),
        }

    def _update_phase_progress(
        self, project_id: str, phase: str, completed_pct: float = 0.0, status: str = "", actual_hours: float = 0.0
    ) -> dict[str, Any]:
        phases = self.ws.ops.phases_for(project_id)
        target = next((p for p in phases if str(p.get("phase", "")) == phase), None)
        if target is None:
            return {
                "error": f"project {project_id} has no phase '{phase}'",
                "available_phases": [p.get("phase") for p in phases],
            }
        pct = min(max(float(completed_pct or 0), 0.0), 100.0)
        changes: dict[str, Any] = {"completed_pct": pct}
        if actual_hours:
            changes["actual_hours"] = float(actual_hours)
        if status:
            changes["status"] = _enum(PhaseStatus, status, PhaseStatus.IN_PROGRESS).value
        elif pct >= 100:
            changes["status"] = PhaseStatus.COMPLETE.value
        elif pct > 0:
            changes["status"] = PhaseStatus.IN_PROGRESS.value
        updated = self.ws.ops.patch_phase(str(target["id"]), changes)
        return {"phase": updated, "unblocked_next_phase": changes.get("status") == PhaseStatus.COMPLETE.value}

    def _upsert_crew_member(
        self,
        name: str,
        employee_id: str = "",
        crew_role: str = "",
        phone: str = "",
        email: str = "",
        skills: dict[str, Any] | None = None,
        certifications: dict[str, Any] | None = None,
        productivity_factor: float = 0.0,
        reliability: float = 0.0,
        can_lead: bool = False,
        works_alone: bool = True,
        home_base: str = "",
        max_hours_per_week: float = 0.0,
        active: bool = True,
        development_notes: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        unknown_skills = sorted(set(skills or {}) - set(SKILLS))
        unknown_certs = sorted(set(certifications or {}) - set(CERTIFICATIONS))

        employee = Employee(
            name=name,
            crew_role=_enum(CrewRole, crew_role, CrewRole.PAINTER),
            phone=phone,
            email=email,
            skills={k: int(v or 0) for k, v in (skills or {}).items()},
            certifications={k: str(v or "") for k, v in (certifications or {}).items()},
            productivity_factor=float(productivity_factor or 1.0),
            reliability=float(reliability or 1.0),
            can_lead=bool(can_lead),
            works_alone=bool(works_alone),
            home_base=home_base,
            max_hours_per_week=float(max_hours_per_week or 44.0),
            active=bool(active),
            development_notes=development_notes,
            notes=notes,
            id=employee_id or Employee(name=name).id,
        )
        record = self.ws.ops.upsert_employee(employee.to_dict())
        result: dict[str, Any] = {"employee": record}
        if unknown_skills:
            result["unrecognised_skills"] = unknown_skills
            result["known_skills"] = list(SKILLS)
        if unknown_certs:
            result["unrecognised_certifications"] = unknown_certs
            result["known_certifications"] = list(CERTIFICATIONS)
        result["reminder"] = (
            "Skills and reliability are evidence-based and reviewable. Record what the work "
            "shows, not an impression, and never a protected personal characteristic."
        )
        return result

    def _record_time_off(
        self, employee_id: str, start_date: str, end_date: str = "", kind: str = "vacation",
        status: str = "approved", note: str = "",
    ) -> dict[str, Any]:
        if self.ws.ops.get_employee(employee_id) is None:
            return {"error": f"no employee with id {employee_id}"}
        start = _parse_date(start_date, "start_date")
        if isinstance(start, str):
            return {"error": start}
        end = _parse_date(end_date, "end_date") if end_date else start
        if isinstance(end, str):
            return {"error": end}
        if end < start:
            return {"error": "end_date is before start_date"}

        record = TimeOff(
            employee_id=employee_id,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            kind=_enum(TimeOffKind, kind, TimeOffKind.VACATION),
            status=_enum(TimeOffStatus, status, TimeOffStatus.APPROVED),
            note=note,
        )
        saved = self.ws.ops.record_time_off(record.to_dict())

        clashes = [
            a for a in self.ws.ops.assignments_between(start, end, employee_id=employee_id)
        ]
        return {
            "time_off": saved,
            "conflicting_shifts": clashes,
            "action_required": (
                f"{len(clashes)} existing shift(s) fall inside this time off and must be "
                "re-staffed or cancelled."
                if clashes
                else "No existing shifts conflict."
            ),
        }

    # --- scheduling actions (Level 2) --------------------------------------

    def _assign_crew(
        self,
        project_id: str,
        employee_id: str,
        work_date: str,
        phase: str = "",
        hours: float = STANDARD_HOURS_PER_DAY,
        is_lead: bool = False,
        rationale: str = "",
        approval_id: str = "",
    ) -> dict[str, Any]:
        """Write one draft shift, but only if every hard constraint passes.

        `approval_id` is supplied by the harness when this runs out of the
        approval queue. A value the model puts in the arguments is discarded
        by `Toolbox.call`, so this is not a bypass.
        """
        day = _parse_date(work_date, "work_date")
        if isinstance(day, str):
            return {"error": day}

        check = check_assignment(self.ws, project_id, employee_id, day, float(hours))
        if check.blocks:
            return {
                "status": "refused",
                "reason": "hard constraint",
                "blocks": check.blocks,
                "warnings": check.warnings,
                "instruction": (
                    "This shift was NOT created and cannot be approved into existence. "
                    "Change the plan: free the day, pick another person, or move the work."
                ),
            }
        if check.exceptions and not approval_id:
            return {
                "status": "refused",
                "reason": "management approval required",
                "exceptions": check.exceptions,
                "warnings": check.warnings,
                "instruction": (
                    "This shift was NOT created. Call request_scheduling_exception with the "
                    "same details and a justification; it will be queued for a human. Do not "
                    "tell anyone this shift is scheduled."
                ),
            }

        if phase:
            known = {p.get("phase") for p in self.ws.ops.phases_for(project_id)}
            if known and phase not in known:
                return {"error": f"'{phase}' is not a phase on this project", "available_phases": sorted(known)}

        assignment = Assignment(
            project_id=project_id,
            employee_id=employee_id,
            work_date=day.isoformat(),
            phase=phase,
            hours=float(hours),
            is_lead=bool(is_lead),
            status=AssignmentStatus.DRAFT,
            rationale=rationale,
            approval_id=approval_id,
        )
        saved = self.ws.ops.upsert_assignment(assignment.to_dict())
        return {
            "assignment": saved,
            "status": "draft",
            "warnings": check.warnings,
            "exception_approved_under": approval_id or None,
            "instruction": (
                "This shift is a DRAFT. It is not a commitment to the employee or the client "
                "until confirm_shifts runs and the crew has been notified."
            ),
        }

    def _commit_week_plan(self, start_date: str = "", days: int = 7, include_weekends: bool = False) -> dict[str, Any]:
        """Write the provisional week plan as draft shifts, checking each one."""
        start = _parse_date(start_date, "start_date") if start_date else date.today()
        if isinstance(start, str):
            return {"error": start}

        plan = plan_week(self.ws, start, days=int(days or 7), include_weekends=bool(include_weekends))
        created: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []

        for shift in plan["shifts"]:
            for member in shift["crew"]:
                result = self._assign_crew(
                    project_id=str(shift["project_id"]),
                    employee_id=str(member["employee_id"]),
                    work_date=str(shift["date"]),
                    phase=str(shift.get("phase", "")),
                    hours=float(shift.get("hours_each", STANDARD_HOURS_PER_DAY)),
                    is_lead=bool(member.get("is_lead", False)),
                    rationale=f"Week plan: fit score {member.get('fit_score')}/100.",
                )
                if result.get("status") == "draft":
                    created.append(result["assignment"])
                else:
                    refused.append(
                        {
                            "date": shift["date"],
                            "project": shift["project"],
                            "employee": member["name"],
                            "reason": result.get("reason", result.get("error", "unknown")),
                            "detail": result.get("blocks") or result.get("exceptions") or [],
                        }
                    )

        return {
            "window": plan["window"],
            "drafts_created": len(created),
            "refused": refused,
            "unstaffed": plan["unstaffed"],
            "assignments": created,
            "instruction": (
                "All created shifts are DRAFTS. Review, then confirm_shifts and notify the "
                "crew. Report the refused list honestly — those days are not covered."
            ),
        }

    def _confirm_shifts(self, assignment_ids: list[str] | None = None, project_id: str = "", work_date: str = "") -> dict[str, Any]:
        """Turn drafts into confirmed shifts — the point of commitment."""
        targets: list[dict[str, Any]] = []
        if assignment_ids:
            for assignment_id in assignment_ids:
                record = self.ws.ops.get_assignment(str(assignment_id))
                if record is None:
                    return {"error": f"no assignment with id {assignment_id}"}
                targets.append(record)
        elif project_id:
            day = _parse_date(work_date, "work_date") if work_date else None
            if isinstance(day, str):
                return {"error": day}
            targets = [
                a for a in self.ws.ops.assignments_for_project(project_id)
                if a.get("status") == AssignmentStatus.DRAFT.value
                and (day is None or str(a.get("work_date", "")) == day.isoformat())
            ]
        else:
            return {"error": "supply assignment_ids, or a project_id (optionally with work_date)"}

        confirmed, rechecked = [], []
        for record in targets:
            if record.get("status") != AssignmentStatus.DRAFT.value:
                continue
            day = _parse_date(str(record.get("work_date", "")))
            if isinstance(day, str):
                continue
            # Re-check at the point of commitment: the world may have moved
            # since the draft was written.
            check = check_assignment(
                self.ws,
                str(record.get("project_id", "")),
                str(record.get("employee_id", "")),
                day,
                float(record.get("hours", STANDARD_HOURS_PER_DAY) or STANDARD_HOURS_PER_DAY),
                excluding_assignment=str(record.get("id", "")),
            )
            if check.blocks or (check.exceptions and not record.get("approval_id")):
                rechecked.append(
                    {
                        "assignment_id": record.get("id"),
                        "work_date": record.get("work_date"),
                        "blocks": check.blocks,
                        "exceptions": check.exceptions,
                    }
                )
                continue
            updated = self.ws.ops.patch_assignment(
                str(record["id"]), {"status": AssignmentStatus.CONFIRMED.value}
            )
            if updated:
                confirmed.append(updated)

        return {
            "confirmed": len(confirmed),
            "assignments": confirmed,
            "blocked_on_recheck": rechecked,
            "next_step": (
                "Send the crew briefing with send_crew_schedule. A confirmed shift nobody "
                "has been told about is not a schedule."
                if confirmed
                else "Nothing was confirmed."
            ),
        }

    def _cancel_shift(self, assignment_id: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            return {"error": "a cancellation needs a reason — it goes in the audit record"}
        record = self.ws.ops.get_assignment(assignment_id)
        if record is None:
            return {"error": f"no assignment with id {assignment_id}"}
        was_confirmed = record.get("status") == AssignmentStatus.CONFIRMED.value
        cancelled = self.ws.ops.cancel_assignment(assignment_id, reason)
        return {
            "assignment": cancelled,
            "was_confirmed": was_confirmed,
            "action_required": (
                "This shift was confirmed. The employee must be told it is cancelled, and the "
                "project is now short-crewed for that day — re-staff it or report the gap."
                if was_confirmed
                else "Draft removed from the plan; nobody had been told."
            ),
        }

    def _record_time_entry(
        self, employee_id: str, work_date: str, clock_in: str = "", clock_out: str = "", project_id: str = "", source: str = "manual",
    ) -> dict[str, Any]:
        """Record a real clock-in/clock-out. Never a guess."""
        if self.ws.ops.get_employee(employee_id) is None:
            return {"error": f"no employee with id {employee_id}"}
        day = _parse_date(work_date, "work_date")
        if isinstance(day, str):
            return {"error": day}

        entry = TimeEntry(
            employee_id=employee_id,
            work_date=day.isoformat(),
            clock_in=clock_in,
            clock_out=clock_out,
            project_id=project_id,
            source=source,
        )
        if clock_in and clock_out and entry.hours <= 0:
            return {"error": "clock_out is not after clock_in; the entry was not recorded"}
        saved = self.ws.ops.record_time_entry(entry.to_dict())

        # Actual hours are derived from recorded entries, never typed in.
        result: dict[str, Any] = {"time_entry": saved}
        if project_id and self.ws.ops.get_project(project_id):
            total = sum(
                float(e.get("hours", 0) or 0)
                for e in self.ws.ops.time_entries_between(day - timedelta(days=730), day + timedelta(days=1), project_id=project_id)
            )
            updated = self.ws.ops.patch_project(project_id, {"actual_hours": round(total, 2)})
            result["project_actual_hours"] = (updated or {}).get("actual_hours")
            budget = float((updated or {}).get("labour_budget_hours", 0) or 0)
            if budget and total > budget:
                result["budget_alert"] = (
                    f"Project is now {total:.1f}h against a {budget:.0f}h labour budget."
                )

        scheduled = self.ws.ops.assignments_on(day, employee_id=employee_id)
        if not scheduled:
            result["note"] = "No shift was scheduled for this person on this date — worth checking why."
        return result

    def _book_equipment(self, equipment_id: str, project_id: str, start_date: str, end_date: str = "") -> dict[str, Any]:
        start = _parse_date(start_date, "start_date")
        if isinstance(start, str):
            return {"error": start}
        end = _parse_date(end_date, "end_date") if end_date else start
        if isinstance(end, str):
            return {"error": end}

        booking = EquipmentBooking(
            equipment_id=equipment_id,
            project_id=project_id,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )
        clashes = [
            b for b in self.ws.ops.equipment_bookings(equipment_id)
            if str(b.get("start_date", "")) <= end.isoformat()
            and start.isoformat() <= str(b.get("end_date", "") or b.get("start_date", ""))
        ]
        if clashes:
            return {
                "status": "refused",
                "reason": "equipment is already booked over these dates",
                "conflicts": clashes,
                "instruction": "Re-sequence one project or arrange a rental. Do not double-book kit.",
            }
        return {"booking": self.ws.ops.book_equipment(booking.to_dict()), "status": "booked"}

    # --- communication -----------------------------------------------------

    def _send_crew_schedule(
        self,
        project_id: str,
        work_date: str,
        planned_work: str,
        equipment_and_ppe: str = "",
        reporting: str = "Daily report and progress photos by end of shift.",
        contact: str = "",
        extra_notes: str = "",
    ) -> dict[str, Any]:
        """Brief the crew on a confirmed shift.

        The message is assembled here so it always carries every field the
        communication rules require — a briefing missing the address or the
        lead is the failure this tool exists to prevent.
        """
        project = self.ws.ops.get_project(project_id)
        if project is None:
            return {"error": f"no project with id {project_id}"}
        day = _parse_date(work_date, "work_date")
        if isinstance(day, str):
            return {"error": day}

        shifts = [
            a for a in self.ws.ops.assignments_between(day, day, project_id=project_id)
            if a.get("status") == AssignmentStatus.CONFIRMED.value
        ]
        if not shifts:
            drafts = [
                a for a in self.ws.ops.assignments_between(day, day, project_id=project_id)
                if a.get("status") == AssignmentStatus.DRAFT.value
            ]
            return {
                "status": "refused",
                "reason": (
                    f"{len(drafts)} draft shift(s) but nothing confirmed for {day.isoformat()}."
                    if drafts
                    else f"No confirmed shifts on {day.isoformat()}."
                ),
                "instruction": "Confirm the shifts first. Never brief a crew on a tentative schedule.",
            }
        if not self.ws.settings.company_email:
            return {"error": "COMPANY_EMAIL is not set; refusing to send from an unknown address"}

        crew, lead_name, recipients = [], "", []
        for shift in shifts:
            person = self.ws.ops.get_employee(str(shift.get("employee_id", ""))) or {}
            crew.append(str(person.get("name", "?")))
            if shift.get("is_lead"):
                lead_name = str(person.get("name", ""))
            if person.get("email"):
                recipients.append(str(person["email"]))

        body = _crew_briefing(
            project=project,
            day=day,
            crew=crew,
            lead_name=lead_name or "not assigned — confirm before start",
            planned_work=planned_work,
            equipment_and_ppe=equipment_and_ppe or "Standard PPE. Confirm site-specific requirements with the lead.",
            reporting=reporting,
            contact=contact or self.ws.settings.company_phone or self.ws.settings.company_email,
            extra_notes=extra_notes,
        )

        if not recipients:
            return {
                "status": "not_sent",
                "reason": "no email addresses on file for the assigned crew",
                "message": body,
                "instruction": "Add contact details, or deliver this briefing another way. It was NOT sent.",
            }

        sends = [
            self.ws.email.send(
                to=address,
                subject=f"Schedule — {project.get('name', '')} — {day.isoformat()}",
                body=body,
                from_email=self.ws.settings.company_email,
            )
            for address in sorted(set(recipients))
        ]
        return {
            "status": "sent",
            "recipients": sorted(set(recipients)),
            "shifts_covered": len(shifts),
            "message": body,
            "delivery": sends,
        }

    def _draft_crew_message(
        self, project_id: str, work_date: str, planned_work: str, equipment_and_ppe: str = "", extra_notes: str = ""
    ) -> dict[str, Any]:
        """Compose the briefing without sending it — for review."""
        project = self.ws.ops.get_project(project_id)
        if project is None:
            return {"error": f"no project with id {project_id}"}
        day = _parse_date(work_date, "work_date")
        if isinstance(day, str):
            return {"error": day}

        shifts = self.ws.ops.assignments_between(day, day, project_id=project_id)
        crew, lead_name = [], ""
        for shift in shifts:
            person = self.ws.ops.get_employee(str(shift.get("employee_id", ""))) or {}
            crew.append(str(person.get("name", "?")))
            if shift.get("is_lead"):
                lead_name = str(person.get("name", ""))

        return {
            "draft": _crew_briefing(
                project=project,
                day=day,
                crew=crew or ["(none assigned)"],
                lead_name=lead_name or "not assigned",
                planned_work=planned_work,
                equipment_and_ppe=equipment_and_ppe or "Standard PPE.",
                reporting="Daily report and progress photos by end of shift.",
                contact=self.ws.settings.company_phone or self.ws.settings.company_email,
                extra_notes=extra_notes,
            ),
            "all_shifts_confirmed": bool(shifts) and all(
                s.get("status") == AssignmentStatus.CONFIRMED.value for s in shifts
            ),
            "status": "draft_only_not_sent",
        }

    # --- management decisions (Level 3) -------------------------------------

    def _request_scheduling_exception(
        self,
        project_id: str,
        employee_id: str,
        work_date: str,
        justification: str,
        phase: str = "",
        hours: float = STANDARD_HOURS_PER_DAY,
        is_lead: bool = False,
        approval_id: str = "",
    ) -> dict[str, Any]:
        """Assign someone despite an approved exception. Level 3 — never autonomous.

        Reaching the body of this function means a human approved it, because
        the autonomy gate queues the call otherwise.
        """
        if not justification.strip():
            return {"error": "an exception needs a justification; it is the record of why"}
        day = _parse_date(work_date, "work_date")
        if isinstance(day, str):
            return {"error": day}

        check = check_assignment(self.ws, project_id, employee_id, day, float(hours))
        if check.blocks:
            return {
                "status": "refused",
                "reason": "hard constraint — not an approvable exception",
                "blocks": check.blocks,
                "instruction": (
                    "Approval cannot create this shift. Double-booking and the working-hour "
                    "safety caps have no override."
                ),
            }
        return self._assign_crew(
            project_id=project_id,
            employee_id=employee_id,
            work_date=work_date,
            phase=phase,
            hours=hours,
            is_lead=is_lead,
            rationale=f"Management exception: {justification}",
            approval_id=approval_id or "approved",
        )

    def _postpone_project(self, project_id: str, new_start_date: str, reason: str, approval_id: str = "") -> dict[str, Any]:
        """Move or hold a confirmed project. Level 3 — it changes a client commitment."""
        project = self.ws.ops.get_project(project_id)
        if project is None:
            return {"error": f"no project with id {project_id}"}
        if not reason.strip():
            return {"error": "postponing a project needs a reason"}
        start = _parse_date(new_start_date, "new_start_date")
        if isinstance(start, str):
            return {"error": start}

        affected = [
            a for a in self.ws.ops.assignments_for_project(project_id)
            if str(a.get("status", "")) in {AssignmentStatus.DRAFT.value, AssignmentStatus.CONFIRMED.value}
            and str(a.get("work_date", "")) < start.isoformat()
        ]
        for shift in affected:
            self.ws.ops.cancel_assignment(str(shift["id"]), f"Project postponed: {reason}")

        updated = self.ws.ops.patch_project(
            project_id, {"start_date": start.isoformat(), "scope_notes": f"{project.get('scope_notes', '')}\nPostponed: {reason}".strip()}
        )
        return {
            "project": updated,
            "shifts_cancelled": len(affected),
            "approval_id": approval_id or "approved",
            "next_step": (
                "Tell the affected crew their shifts are cancelled, and notify the client "
                "with notify_client_schedule_change."
            ),
        }

    def _notify_client_schedule_change(
        self,
        project_id: str,
        change: str,
        reason: str,
        impact: str,
        next_step: str,
        to: str = "",
        coordination_required: str = "",
        approval_id: str = "",
    ) -> dict[str, Any]:
        """Tell a client or GC about a schedule change. Level 3 — it is a commitment.

        The body is composed from these fields only. There is deliberately no
        free-text channel, so internal costs, employee detail and internal
        assessments cannot travel out through this tool.
        """
        project = self.ws.ops.get_project(project_id)
        if project is None:
            return {"error": f"no project with id {project_id}"}
        recipient = to or str(project.get("client_email", ""))
        if not recipient:
            return {"error": "no client email on the project and none supplied"}
        if not self.ws.settings.company_email:
            return {"error": "COMPANY_EMAIL is not set; refusing to send from an unknown address"}

        lines = [
            f"Re: {project.get('name', '')}"
            + (f" — {project.get('address', '')}" if project.get("address") else ""),
            "",
            f"Schedule change: {change}",
            f"Reason: {reason}",
            f"Expected impact: {impact}",
        ]
        if coordination_required:
            lines.append(f"Coordination required: {coordination_required}")
        lines += ["", f"Next step: {next_step}", "", self.ws.settings.company_name]

        body = "\n".join(lines)
        result = self.ws.email.send(
            to=recipient,
            subject=f"Schedule update — {project.get('name', '')}",
            body=body,
            from_email=self.ws.settings.company_email,
        )
        return {
            "status": "sent",
            "to": recipient,
            "message": body,
            "approval_id": approval_id or "approved",
            "delivery": result,
        }

    def _request_subcontractor(
        self, project_id: str, trade: str, days_needed: int, reason: str, estimated_cost: float = 0.0, approval_id: str = ""
    ) -> dict[str, Any]:
        """Bring in temporary labour or a subcontractor. Level 3 — it commits cost."""
        project = self.ws.ops.get_project(project_id)
        if project is None:
            return {"error": f"no project with id {project_id}"}
        return {
            "status": "authorized",
            "project": project.get("name", ""),
            "trade": trade,
            "days_needed": int(days_needed or 0),
            "reason": reason,
            "estimated_cost": float(estimated_cost or 0),
            "approval_id": approval_id or "approved",
            "next_step": (
                "Add the subcontractor to the roster with upsert_crew_member so their shifts "
                "are constraint-checked like anyone else's."
            ),
        }

    # --- registration --------------------------------------------------------

    def register_into(self, toolbox: Any) -> None:
        from .tools import array, boolean, integer, number, obj, string

        r = toolbox.register
        phase_names = [p.value for p in Phase]

        r(
            "list_projects",
            "List projects that need crew, already ranked by the scheduling priority order "
            "with deadline pressure computed. Start every scheduling cycle here.",
            obj({
                "status": string("Optional status filter", [s.value for s in ProjectStatus]),
                "limit": integer("How many to return (default 25)"),
            }),
            self._list_projects,
        )
        r(
            "find_projects",
            "Search projects by name, address, client or note text.",
            obj({"term": string("Search text")}, ["term"]),
            self._find_projects,
        )
        r(
            "get_project",
            "Full project record: scope, phases with progress, every shift booked against it, "
            "and whether it needs a lead on site.",
            obj({"project_id": string("The proj_... id")}, ["project_id"]),
            self._get_project,
        )
        r(
            "project_schedule",
            "The shifts booked on one project, grouped by day, with who is on each and whether "
            "the shift is draft or confirmed.",
            obj({
                "project_id": string("The proj_... id"),
                "start_date": string("ISO date to start from, defaults to today"),
                "days": integer("Window length in days (default 14)"),
            }, ["project_id"]),
            self._project_schedule,
        )
        r(
            "list_crew",
            "The roster with skills and certifications, or — when available_on is given — who "
            "is free that day and who is not, with the reason. Internal data: never forward it.",
            obj({
                "available_on": string("ISO date to check availability for"),
                "skill": string("Only crew with this skill", list(SKILLS)),
            }),
            self._list_crew,
        )
        r(
            "get_crew_member",
            "One employee: skills, certifications and expiries, approved and requested time off, "
            "hours committed this week, and upcoming shifts.",
            obj({"employee_id": string("The emp_... id")}, ["employee_id"]),
            self._get_crew_member,
        )
        r(
            "crew_availability",
            "Who is available on a specific date and who is not, with reasons. Call this BEFORE "
            "proposing or assigning anyone — never assume someone is free.",
            obj({"work_date": string("ISO date")}, ["work_date"]),
            self._crew_availability,
        )
        r(
            "check_shift",
            "Dry-run one assignment before making it. Returns hard blocks, exceptions needing "
            "management approval, and warnings — without writing anything.",
            obj({
                "project_id": string("The proj_... id"),
                "employee_id": string("The emp_... id"),
                "work_date": string("ISO date"),
                "hours": number("Shift hours (default 8)"),
            }, ["project_id", "employee_id", "work_date"]),
            self._check_shift,
        )
        r(
            "propose_crew",
            "Recommend the strongest available crew for a project on a date, scored on skill "
            "match, productivity, reliability, site familiarity and leadership. Returns "
            "alternates and who was excluded and why. Writes nothing.",
            obj({
                "project_id": string("The proj_... id"),
                "work_date": string("ISO date"),
                "crew_size": integer("Override the project's target crew size"),
            }, ["project_id", "work_date"]),
            self._propose_crew,
        )
        r(
            "plan_seven_day_schedule",
            "Build a provisional crew schedule across the coming working days, respecting "
            "priority order, availability, phase dependencies and overtime limits. Provisional "
            "until committed — report it as a proposal.",
            obj({
                "start_date": string("ISO start date, defaults to today"),
                "days": integer("How many days to plan (default 7)"),
                "include_weekends": boolean("Plan weekend work too (default false)"),
            }),
            self._plan_seven_day_schedule,
        )
        r(
            "scheduling_risk_report",
            "The prioritized risk report: double-bookings, shifts on approved time off, "
            "unstaffed starts, undeliverable deadlines, budget overruns, expiring "
            "certifications, overtime, unconfirmed drafts and missing attendance. Run this at "
            "the start of every cycle and before promising anything.",
            obj({"horizon_days": integer("How far ahead to look (default 14)")}),
            self._risk_report,
        )
        r(
            "schedule_variance_report",
            "Planned against actual: estimated vs recorded hours, estimate accuracy, phase "
            "completion and budget position. This is the evidence for improving future "
            "estimates — use it before changing any productivity assumption.",
            obj({"project_id": string("Limit to one project, or omit for all")}),
            self._variance_report,
        )
        r(
            "check_exterior_weather",
            "Weather risk for exterior scope on a project. Returns UNKNOWN rather than a "
            "forecast when the weather service is not connected — never promise exterior "
            "production on an unknown forecast.",
            obj({
                "project_id": string("The proj_... id"),
                "start_date": string("ISO date, defaults to today"),
                "days": integer("How many days to assess (default 3)"),
            }, ["project_id"]),
            self._weather_check,
        )
        r(
            "upsert_project",
            "Create or update a project: scope, dates, labour estimate and budget, required "
            "skills and certifications, access hours and site conditions.",
            obj({
                "name": string("Project name"),
                "project_id": string("Existing proj_... id when updating"),
                "address": string("Site address"),
                "city": string("City, used for the exterior weather lookup"),
                "client_name": string("Client or general contractor"),
                "client_email": string("Client contact email"),
                "account_id": string("CRM acct_... id this project came from"),
                "status": string("Project status", [s.value for s in ProjectStatus]),
                "priority": integer("Scheduling priority, 1 (safety) to 9 (tentative)"),
                "start_date": string("ISO start date"),
                "deadline": string("ISO completion deadline"),
                "estimated_hours": number("Estimated labour hours"),
                "labour_budget_hours": number("Budgeted labour hours"),
                "crew_size_target": integer("Planned crew size"),
                "required_skills": array("Skills the scope needs"),
                "required_certifications": array("Certifications the site requires"),
                "includes_specialty_coating": boolean("Epoxy or specialty coating in scope"),
                "is_exterior": boolean("Exterior work"),
                "is_occupied": boolean("Occupants on site during work"),
                "site_access_start": string("Earliest site access, e.g. 07:00"),
                "site_access_end": string("Latest site access, e.g. 17:00"),
                "access_notes": string("Parking, keys, security, elevator booking"),
                "blocking_trade": string("Trade that must finish before painting starts"),
                "scope_notes": string("Anything else that affects sequencing"),
            }, ["name"]),
            self._upsert_project,
        )
        r(
            "plan_project_phases",
            "Break a project's labour estimate into production phases with dependencies and "
            "cure windows. Do this before scheduling crew — without it, coatings can be "
            "sequenced impossibly.",
            obj({"project_id": string("The proj_... id")}, ["project_id"]),
            self._plan_project_phases,
        )
        r(
            "update_phase_progress",
            "Record real progress on a phase. Completing a phase unblocks the ones that depend "
            "on it. Record only what a report or photo actually shows.",
            obj({
                "project_id": string("The proj_... id"),
                "phase": string("Which phase", phase_names),
                "completed_pct": number("Percent complete, 0-100"),
                "status": string("Override the status", [s.value for s in PhaseStatus]),
                "actual_hours": number("Hours actually spent on the phase so far"),
            }, ["project_id", "phase"]),
            self._update_phase_progress,
        )
        r(
            "upsert_crew_member",
            "Create or update an employee: role, job-relevant skill levels (0-4), "
            "certifications with expiry dates, measured productivity and reliability. Record "
            "evidence, never impressions, and never a protected personal characteristic.",
            obj({
                "name": string("Full name"),
                "employee_id": string("Existing emp_... id when updating"),
                "crew_role": string("Role", [r.value for r in CrewRole]),
                "phone": string("Phone number"),
                "email": string("Email address"),
                "skills": {
                    "type": "object",
                    "description": f"Skill levels 0-4 ({', '.join(SKILLS)})",
                    "additionalProperties": {"type": "integer"},
                },
                "certifications": {
                    "type": "object",
                    "description": f"Certification to ISO expiry date ({', '.join(CERTIFICATIONS)}). Empty string means no expiry.",
                    "additionalProperties": {"type": "string"},
                },
                "productivity_factor": number("Measured output vs baseline; 1.0 is on-standard"),
                "reliability": number("Attendance and punctuality, 0 to 1"),
                "can_lead": boolean("Qualified to lead a crew"),
                "works_alone": boolean("Can work a site unsupervised"),
                "home_base": string("Where they travel from"),
                "max_hours_per_week": number("Their weekly hour limit"),
                "active": boolean("On the active roster"),
                "development_notes": string("Documented, reviewable coaching needs"),
                "notes": string("Other job-relevant notes"),
            }, ["name"]),
            self._upsert_crew_member,
        )
        r(
            "record_time_off",
            "Record vacation, sick leave, appointments or training. Approved time off becomes a "
            "hard constraint the scheduler cannot book over. Returns any shifts it now conflicts with.",
            obj({
                "employee_id": string("The emp_... id"),
                "start_date": string("ISO first day off"),
                "end_date": string("ISO last day off, defaults to start_date"),
                "kind": string("Type of leave", [k.value for k in TimeOffKind]),
                "status": string("Approval status", [s.value for s in TimeOffStatus]),
                "note": string("Context"),
            }, ["employee_id", "start_date"]),
            self._record_time_off,
        )
        r(
            "assign_crew",
            "Put one person on one project for one day, as a DRAFT shift. Every hard constraint "
            "is checked first: double-booking, daily and weekly hour caps, approved time off and "
            "required certifications. Anything needing a management decision is refused with the "
            "reason — it is not created.",
            obj({
                "project_id": string("The proj_... id"),
                "employee_id": string("The emp_... id"),
                "work_date": string("ISO date"),
                "phase": string("Which production phase", phase_names),
                "hours": number("Shift hours (default 8)"),
                "is_lead": boolean("This person leads the crew that day"),
                "rationale": string("Why this person on this project — kept in the audit record"),
            }, ["project_id", "employee_id", "work_date"]),
            self._assign_crew,
        )
        r(
            "commit_week_plan",
            "Write the provisional week plan out as draft shifts, re-checking every one. "
            "Returns what was created, what was refused and why, and which days stay uncovered.",
            obj({
                "start_date": string("ISO start date, defaults to today"),
                "days": integer("How many days (default 7)"),
                "include_weekends": boolean("Include weekend work"),
            }),
            self._commit_week_plan,
        )
        r(
            "confirm_shifts",
            "Turn draft shifts into confirmed ones — the point at which the schedule becomes a "
            "commitment. Every shift is re-checked at confirmation in case the world moved.",
            obj({
                "assignment_ids": array("Specific asg_... ids to confirm"),
                "project_id": string("Confirm this project's drafts instead"),
                "work_date": string("Limit to one ISO date"),
            }),
            self._confirm_shifts,
        )
        r(
            "cancel_shift",
            "Cancel a shift with a reason. If it was confirmed, the employee must be told and "
            "the day is now uncovered.",
            obj({
                "assignment_id": string("The asg_... id"),
                "reason": string("Why it is being cancelled"),
            }, ["assignment_id", "reason"]),
            self._cancel_shift,
        )
        r(
            "record_time_entry",
            "Record an actual clock-in and clock-out. Project actual hours are recomputed from "
            "recorded entries — never type in an hours figure that was not worked.",
            obj({
                "employee_id": string("The emp_... id"),
                "work_date": string("ISO date"),
                "clock_in": string("24h time, e.g. 07:05"),
                "clock_out": string("24h time, e.g. 15:40"),
                "project_id": string("Project worked on"),
                "source": string("Where this came from, e.g. timeclock or daily report"),
            }, ["employee_id", "work_date"]),
            self._record_time_entry,
        )
        r(
            "book_equipment",
            "Reserve a sprayer, lift, sander or vehicle for a project over a date range. "
            "Refuses if it is already booked — kit in two places stops production.",
            obj({
                "equipment_id": string("The eqp_... id"),
                "project_id": string("The proj_... id"),
                "start_date": string("ISO first day"),
                "end_date": string("ISO last day, defaults to start_date"),
            }, ["equipment_id", "project_id", "start_date"]),
            self._book_equipment,
        )
        r(
            "draft_crew_message",
            "Compose the crew briefing for review without sending it.",
            obj({
                "project_id": string("The proj_... id"),
                "work_date": string("ISO date"),
                "planned_work": string("What the crew is doing that day"),
                "equipment_and_ppe": string("Equipment and PPE required"),
                "extra_notes": string("Anything else the crew needs"),
            }, ["project_id", "work_date", "planned_work"]),
            self._draft_crew_message,
        )
        r(
            "send_crew_schedule",
            "Send the crew their briefing for a CONFIRMED shift. The message is assembled with "
            "every required field — project, address, date, start time, crew, lead, work, "
            "equipment and PPE, access, target and reporting. Refuses to brief on drafts.",
            obj({
                "project_id": string("The proj_... id"),
                "work_date": string("ISO date"),
                "planned_work": string("What the crew is doing that day"),
                "equipment_and_ppe": string("Equipment and PPE required"),
                "reporting": string("What the crew must report and when"),
                "contact": string("Who to contact with questions"),
                "extra_notes": string("Anything else the crew needs"),
            }, ["project_id", "work_date", "planned_work"]),
            self._send_crew_schedule,
        )
        r(
            "request_scheduling_exception",
            "Ask management to approve a shift that a constraint blocks — working an approved "
            "day off, assigning past a certification, or overtime beyond the limit. Requires "
            "human approval; it never executes on its own. Hard constraints like double-booking "
            "cannot be approved and are refused outright.",
            obj({
                "project_id": string("The proj_... id"),
                "employee_id": string("The emp_... id"),
                "work_date": string("ISO date"),
                "justification": string("Why this exception is warranted, and what it costs"),
                "phase": string("Which production phase", phase_names),
                "hours": number("Shift hours"),
                "is_lead": boolean("This person leads that day"),
            }, ["project_id", "employee_id", "work_date", "justification"]),
            self._request_scheduling_exception,
        )
        r(
            "postpone_project",
            "Move a project's start date and cancel the shifts before it. Requires human "
            "approval — it changes a client commitment.",
            obj({
                "project_id": string("The proj_... id"),
                "new_start_date": string("ISO new start date"),
                "reason": string("Why the project is moving"),
            }, ["project_id", "new_start_date", "reason"]),
            self._postpone_project,
        )
        r(
            "notify_client_schedule_change",
            "Tell a client or general contractor about a schedule change. Requires human "
            "approval. The message is built from these fields only, so internal costs, "
            "employee information and internal assessments cannot leave through it.",
            obj({
                "project_id": string("The proj_... id"),
                "change": string("What is changing, in plain terms"),
                "reason": string("Why, at a level appropriate for the client"),
                "impact": string("Expected impact on completion"),
                "next_step": string("What you are asking them to do or confirm"),
                "to": string("Recipient email; defaults to the project's client email"),
                "coordination_required": string("Access or trade coordination needed"),
            }, ["project_id", "change", "reason", "impact", "next_step"]),
            self._notify_client_schedule_change,
        )
        r(
            "request_subcontractor",
            "Bring in temporary labour or a subcontractor. Requires human approval — it commits "
            "cost the company has not agreed to.",
            obj({
                "project_id": string("The proj_... id"),
                "trade": string("What they are needed for"),
                "days_needed": integer("How many days"),
                "reason": string("Why internal crew cannot cover it"),
                "estimated_cost": number("Estimated cost"),
            }, ["project_id", "trade", "days_needed", "reason"]),
            self._request_subcontractor,
        )


def _crew_briefing(
    *,
    project: dict[str, Any],
    day: date,
    crew: list[str],
    lead_name: str,
    planned_work: str,
    equipment_and_ppe: str,
    reporting: str,
    contact: str,
    extra_notes: str = "",
) -> str:
    """Assemble a crew briefing carrying every required field."""
    target = project.get("deadline") or "see project schedule"
    lines = [
        f"{project.get('name', '')} — {day.strftime('%A %d %B %Y')}",
        "",
        f"Address:        {project.get('address', '') or 'TBC — confirm before travelling'}",
        f"Start time:     {project.get('site_access_start', '07:00')}",
        f"Site access:    {project.get('site_access_start', '07:00')} to {project.get('site_access_end', '17:00')}",
        f"Crew:           {', '.join(crew)}",
        f"Team lead:      {lead_name}",
        "",
        f"Planned work:   {planned_work}",
        f"Equipment/PPE:  {equipment_and_ppe}",
        f"Access notes:   {project.get('access_notes', '') or 'None recorded.'}",
        f"Completion target: {target}",
    ]
    if project.get("is_occupied"):
        lines.append("Occupied site:  occupants present — keep areas clear, contained and tidy.")
    if project.get("blocking_trade"):
        lines.append(f"Trade coordination: {project['blocking_trade']} must be clear of the area.")
    if extra_notes:
        lines.append(f"Notes:          {extra_notes}")
    lines += ["", f"Reporting:      {reporting}", f"Questions:      {contact}"]
    return "\n".join(lines)
