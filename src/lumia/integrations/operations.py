"""Operations integration — the record of truth for projects, people and shifts.

Same contract as the CRM: writes land in the local store first so
read-after-write always holds, then mirror best-effort to a hosted
scheduling system when one is configured. An outage in the hosted system
must never lose an assignment or, worse, leave the agent believing someone
is free when they are not.

Every read here is a fact the agent is allowed to rely on. Everything the
agent cannot get from here is UNKNOWN, and the tools say so.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from ..domain.workforce import (
    ACTIVE_ASSIGNMENT_STATUSES,
    AssignmentStatus,
    TimeOffStatus,
    week_start,
)
from ..store import LocalStore
from .base import Integration, IntegrationError

log = logging.getLogger(__name__)


class OperationsSystem(Integration):
    """Projects, phases, employees, assignments, time off and equipment."""

    def __init__(self, credentials, store: LocalStore) -> None:  # type: ignore[no-untyped-def]
        super().__init__(credentials=credentials)
        self.store = store

    def _mirror(self, method: str, path: str, payload: dict[str, Any]) -> None:
        if not self.live:
            return
        try:
            self.request(method, path, json=payload, mock={})
        except IntegrationError as exc:
            log.warning("operations mirror to %s failed, local store is authoritative: %s", path, exc)

    # --- projects -------------------------------------------------------

    def upsert_project(self, project: dict[str, Any]) -> dict[str, Any]:
        existing = self.store.get("projects", project["id"])
        if existing:
            existing.update({k: v for k, v in project.items() if v not in (None, "", [], {})})
            record = existing
        else:
            record = dict(project)
        self.store.put("projects", record["id"], record)
        self._mirror("POST", "/projects", record)
        return record

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        return self.store.get("projects", project_id)

    def list_projects(self, **filters: Any) -> list[dict[str, Any]]:
        return self.store.where("projects", **filters) if filters else self.store.list("projects")

    def find_projects(self, term: str) -> list[dict[str, Any]]:
        return self.store.search("projects", term)

    def patch_project(self, project_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        record = self.store.patch("projects", project_id, changes)
        if record is not None:
            self._mirror("PATCH", f"/projects/{project_id}", changes)
        return record

    # --- phases ---------------------------------------------------------

    def put_phases(self, phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for phase in phases:
            self.store.put("project_phases", phase["id"], phase)
        return phases

    def phases_for(self, project_id: str) -> list[dict[str, Any]]:
        records = self.store.where("project_phases", project_id=project_id)
        return sorted(records, key=lambda p: int(p.get("sequence", 0) or 0))

    def patch_phase(self, phase_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        return self.store.patch("project_phases", phase_id, changes)

    # --- employees -------------------------------------------------------

    def upsert_employee(self, employee: dict[str, Any]) -> dict[str, Any]:
        existing = self.store.get("employees", employee["id"])
        if existing:
            existing.update({k: v for k, v in employee.items() if v not in (None, "", [], {})})
            record = existing
        else:
            record = dict(employee)
        self.store.put("employees", record["id"], record)
        self._mirror("POST", "/employees", record)
        return record

    def get_employee(self, employee_id: str) -> dict[str, Any] | None:
        return self.store.get("employees", employee_id)

    def list_employees(self, active_only: bool = True) -> list[dict[str, Any]]:
        records = self.store.list("employees")
        return [r for r in records if r.get("active", True)] if active_only else records

    def find_employees(self, term: str) -> list[dict[str, Any]]:
        return self.store.search("employees", term)

    # --- time off --------------------------------------------------------

    def record_time_off(self, time_off: dict[str, Any]) -> dict[str, Any]:
        self.store.put("time_off", time_off["id"], time_off)
        self._mirror("POST", "/time-off", time_off)
        return time_off

    def time_off_for(self, employee_id: str, approved_only: bool = False) -> list[dict[str, Any]]:
        records = self.store.where("time_off", employee_id=employee_id)
        if approved_only:
            records = [r for r in records if r.get("status") == TimeOffStatus.APPROVED.value]
        return sorted(records, key=lambda r: str(r.get("start_date", "")))

    def time_off_on(self, day: date, approved_only: bool = True) -> list[dict[str, Any]]:
        """Every time-off record covering a given day."""
        iso = day.isoformat()
        found = []
        for record in self.store.list("time_off"):
            if approved_only and record.get("status") != TimeOffStatus.APPROVED.value:
                continue
            start = str(record.get("start_date", ""))
            end = str(record.get("end_date", "") or start)
            if start and start <= iso <= end:
                found.append(record)
        return found

    # --- assignments ------------------------------------------------------

    def upsert_assignment(self, assignment: dict[str, Any]) -> dict[str, Any]:
        self.store.put("assignments", assignment["id"], assignment)
        self._mirror("POST", "/assignments", assignment)
        return assignment

    def get_assignment(self, assignment_id: str) -> dict[str, Any] | None:
        return self.store.get("assignments", assignment_id)

    def patch_assignment(self, assignment_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        record = self.store.patch("assignments", assignment_id, changes)
        if record is not None:
            self._mirror("PATCH", f"/assignments/{assignment_id}", changes)
        return record

    def assignments_on(self, day: date, employee_id: str = "") -> list[dict[str, Any]]:
        """Live assignments for a day. Cancelled ones do not occupy anyone."""
        iso = day.isoformat()
        records = [
            r
            for r in self.store.list("assignments")
            if r.get("work_date") == iso
            and str(r.get("status", "")) in ACTIVE_ASSIGNMENT_STATUSES
        ]
        if employee_id:
            records = [r for r in records if r.get("employee_id") == employee_id]
        return records

    def assignments_between(self, start: date, end: date, employee_id: str = "", project_id: str = "") -> list[dict[str, Any]]:
        first, last = start.isoformat(), end.isoformat()
        records = [
            r
            for r in self.store.list("assignments")
            if first <= str(r.get("work_date", "")) <= last
            and str(r.get("status", "")) in ACTIVE_ASSIGNMENT_STATUSES
        ]
        if employee_id:
            records = [r for r in records if r.get("employee_id") == employee_id]
        if project_id:
            records = [r for r in records if r.get("project_id") == project_id]
        return sorted(records, key=lambda r: (str(r.get("work_date", "")), str(r.get("employee_id", ""))))

    def assignments_for_project(self, project_id: str) -> list[dict[str, Any]]:
        records = self.store.where("assignments", project_id=project_id)
        return sorted(records, key=lambda r: str(r.get("work_date", "")))

    def scheduled_hours_in_week(self, employee_id: str, day: date, excluding: str = "") -> float:
        """Hours already committed in the Monday-anchored week containing `day`."""
        start = week_start(day)
        records = self.assignments_between(start, start + timedelta(days=6), employee_id=employee_id)
        return round(
            sum(float(r.get("hours", 0) or 0) for r in records if r.get("id") != excluding), 2
        )

    def days_worked_on_project(self, employee_id: str, project_id: str) -> int:
        """Site familiarity, measured rather than assumed."""
        return len(
            {
                str(r.get("work_date", ""))
                for r in self.store.list("assignments")
                if r.get("employee_id") == employee_id
                and r.get("project_id") == project_id
                and str(r.get("status", "")) in ACTIVE_ASSIGNMENT_STATUSES
            }
        )

    def cancel_assignment(self, assignment_id: str, reason: str) -> dict[str, Any]:
        record = self.patch_assignment(
            assignment_id,
            {"status": AssignmentStatus.CANCELLED.value, "notes": reason},
        )
        return record or {"error": f"no assignment with id {assignment_id}"}

    # --- time entries -----------------------------------------------------

    def record_time_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        self.store.put("time_entries", entry["id"], entry)
        return entry

    def time_entries_on(self, day: date, employee_id: str = "") -> list[dict[str, Any]]:
        iso = day.isoformat()
        records = [r for r in self.store.list("time_entries") if r.get("work_date") == iso]
        if employee_id:
            records = [r for r in records if r.get("employee_id") == employee_id]
        return records

    def time_entries_between(self, start: date, end: date, project_id: str = "") -> list[dict[str, Any]]:
        first, last = start.isoformat(), end.isoformat()
        records = [r for r in self.store.list("time_entries") if first <= str(r.get("work_date", "")) <= last]
        if project_id:
            records = [r for r in records if r.get("project_id") == project_id]
        return records

    # --- equipment --------------------------------------------------------

    def upsert_equipment(self, equipment: dict[str, Any]) -> dict[str, Any]:
        self.store.put("equipment", equipment["id"], equipment)
        return equipment

    def list_equipment(self) -> list[dict[str, Any]]:
        return self.store.list("equipment")

    def book_equipment(self, booking: dict[str, Any]) -> dict[str, Any]:
        self.store.put("equipment_bookings", booking["id"], booking)
        return booking

    def equipment_bookings(self, equipment_id: str = "") -> list[dict[str, Any]]:
        records = self.store.list("equipment_bookings")
        if equipment_id:
            records = [r for r in records if r.get("equipment_id") == equipment_id]
        return records

    # --- rollup -----------------------------------------------------------

    def schedule_summary(self, start: date, days: int = 7) -> dict[str, Any]:
        """Counts only — interpretation belongs to the agent, not to the store."""
        end = start + timedelta(days=max(days - 1, 0))
        assignments = self.assignments_between(start, end)
        projects = {p["id"]: p for p in self.list_projects()}

        by_day: dict[str, int] = {}
        by_project: dict[str, int] = {}
        draft = 0
        for record in assignments:
            day = str(record.get("work_date", ""))
            by_day[day] = by_day.get(day, 0) + 1
            name = str(projects.get(str(record.get("project_id", "")), {}).get("name", "unknown"))
            by_project[name] = by_project.get(name, 0) + 1
            if record.get("status") == AssignmentStatus.DRAFT.value:
                draft += 1

        return {
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "assignments": len(assignments),
            "draft_assignments": draft,
            "confirmed_assignments": len(assignments) - draft,
            "shifts_by_day": dict(sorted(by_day.items())),
            "shifts_by_project": by_project,
            "active_employees": len(self.list_employees()),
            "projects_tracked": len(projects),
        }


class Timeclock(Integration):
    """Clock-in / clock-out feed.

    Unconfigured, this returns nothing rather than plausible-looking punches.
    Fabricated attendance data would be worse than none: the spec forbids
    falsifying clock records, and inventing them in a demo is the same lie.
    """

    def punches(self, day: date) -> dict[str, Any]:
        return self.request(
            "GET",
            "/time-entries",
            params={"date": day.isoformat()},
            mock={"entries": [], "note": "no timeclock connected; attendance is UNKNOWN, not empty"},
        )
