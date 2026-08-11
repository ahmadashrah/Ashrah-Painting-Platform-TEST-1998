"""The scheduling engine's guarantees.

These tests exist because the operating spec's "you must never" list is only
worth anything if it is enforced rather than requested. Each test below
corresponds to a rule the agent is not trusted to follow on its own.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from lumia.domain.crewing import assemble_crew, requires_lead, score_employee_fit
from lumia.domain.projects import (
    Phase,
    PhaseStatus,
    Project,
    ProjectPriority,
    ProjectStatus,
    next_schedulable_phases,
    phase_plan,
)
from lumia.domain.workforce import (
    MAX_HOURS_PER_DAY,
    Assignment,
    AssignmentStatus,
    CrewRole,
    Employee,
    TimeOff,
    TimeOffKind,
    TimeOffStatus,
    week_start,
)
from lumia.scheduling import availability, check_assignment, plan_week, rank_projects, scheduling_risks
from lumia.tools import Toolbox

# A known Monday, so weekday arithmetic in the tests is unambiguous.
MONDAY = date(2026, 8, 17)


# --- fixtures --------------------------------------------------------------


def make_employee(ws, name="Painter", **kwargs) -> str:
    defaults = dict(
        skills={"surface_prep": 3, "cut_and_roll": 3, "fine_finish": 3},
        certifications={"whmis": ""},
        can_lead=True,
    )
    defaults.update(kwargs)
    employee = Employee(name=name, **defaults)
    ws.ops.upsert_employee(employee.to_dict())
    return employee.id


def make_project(ws, name="Test Project", **kwargs) -> str:
    defaults = dict(
        status=ProjectStatus.CONFIRMED,
        estimated_hours=80.0,
        crew_size_target=2,
        start_date=MONDAY.isoformat(),
        deadline=(MONDAY + timedelta(days=30)).isoformat(),
    )
    defaults.update(kwargs)
    project = Project(name=name, **defaults)
    ws.ops.upsert_project(project.to_dict())
    return project.id


@pytest.fixture
def box(ws) -> Toolbox:
    return Toolbox(ws)


# --- phases ----------------------------------------------------------------


def test_phase_plan_distributes_the_whole_estimate(ws):
    project = Project(name="Phased", estimated_hours=200.0)
    phases = phase_plan(project)
    assert sum(p.estimated_hours for p in phases) == pytest.approx(200.0, abs=0.5)
    # Specialty coating is out of scope unless the project says otherwise.
    assert Phase.SPECIALTY_COATING.value not in {p.phase.value for p in phases}


def test_specialty_coating_renormalizes_rather_than_inflating(ws):
    project = Project(name="Epoxy", estimated_hours=200.0, includes_specialty_coating=True)
    phases = phase_plan(project)
    assert Phase.SPECIALTY_COATING.value in {p.phase.value for p in phases}
    assert sum(p.estimated_hours for p in phases) == pytest.approx(200.0, abs=0.5)


def test_a_phase_is_blocked_until_its_predecessor_is_complete():
    """You cannot top-coat over prep that isn't finished."""
    phases = [p.to_dict() for p in phase_plan(Project(name="Seq", estimated_hours=100.0))]
    ready = {p["phase"]: p for p in next_schedulable_phases(phases)}

    assert ready["mobilization"]["schedulable"] is True
    assert ready["prep"]["schedulable"] is False
    assert "inspection" in ready["prep"]["blocked_by"][0]

    for record in phases:
        if record["phase"] in {"mobilization", "inspection"}:
            record["status"] = PhaseStatus.COMPLETE.value
    reopened = {p["phase"]: p for p in next_schedulable_phases(phases)}
    assert reopened["prep"]["schedulable"] is True
    assert reopened["masking"]["schedulable"] is False


# --- crew fit --------------------------------------------------------------


def test_missing_certification_blocks_rather_than_deducts(ws):
    """Points can be traded off. A fall-protection card cannot."""
    project = Project(
        name="Heights", estimated_hours=40.0, required_certifications=["working_at_heights"]
    ).to_dict()
    uncertified = Employee(name="No Card", skills={"exterior": 4}, certifications={"whmis": ""})

    fit = score_employee_fit(uncertified, project, on=MONDAY)
    assert fit.eligible is False
    assert any("working_at_heights" in b for b in fit.blockers)


def test_expired_certification_counts_as_missing(ws):
    project = Project(name="Lift", estimated_hours=40.0, required_certifications=["boom_lift"]).to_dict()
    lapsed = Employee(
        name="Lapsed", certifications={"boom_lift": (MONDAY - timedelta(days=1)).isoformat()}
    )
    assert score_employee_fit(lapsed, project, on=MONDAY).eligible is False

    still_valid = Employee(
        name="Valid", certifications={"boom_lift": (MONDAY + timedelta(days=1)).isoformat()}
    )
    assert score_employee_fit(still_valid, project, on=MONDAY).eligible is True


def test_fit_scoring_is_deterministic_and_explainable(ws):
    project = Project(name="Fit", estimated_hours=40.0, required_skills=["fine_finish"]).to_dict()
    employee = Employee(name="Finisher", skills={"fine_finish": 4}, productivity_factor=1.3, reliability=1.0)

    first = score_employee_fit(employee, project, on=MONDAY)
    second = score_employee_fit(employee, project, on=MONDAY)
    assert first.score == second.score
    assert sum(first.breakdown.values()) == first.score


def test_complex_projects_require_a_lead_and_get_one_first(ws):
    project = Project(name="Occupied", estimated_hours=200.0, crew_size_target=2, is_occupied=True).to_dict()
    assert requires_lead(project) is True

    strong_no_lead = Employee(name="Strong", skills={"fine_finish": 4, "spray": 4}, productivity_factor=1.3, can_lead=False)
    weaker_lead = Employee(name="Leader", skills={"fine_finish": 2}, productivity_factor=0.9, can_lead=True)

    proposal = assemble_crew(project, [(strong_no_lead, 0), (weaker_lead, 0)], on=MONDAY)
    assert proposal.lead_employee_id == weaker_lead.id
    assert {m["employee_name"] for m in proposal.crew} == {"Strong", "Leader"}


def test_an_apprentice_picked_alone_gets_paired_with_a_mentor(ws):
    project = Project(name="Solo", estimated_hours=20.0, crew_size_target=1).to_dict()
    # The apprentice outscores on raw fit, so a size-1 crew would be them alone.
    apprentice = Employee(
        name="Apprentice", crew_role=CrewRole.APPRENTICE, works_alone=False,
        skills={"surface_prep": 4}, productivity_factor=1.3,
    )
    mentor = Employee(name="Mentor", skills={"surface_prep": 1}, can_lead=True, productivity_factor=0.7)

    proposal = assemble_crew(project, [(apprentice, 0), (mentor, 0)], crew_size=1, on=MONDAY)
    assert {m["employee_name"] for m in proposal.crew} == {"Apprentice", "Mentor"}
    assert any("should not" in w for w in proposal.warnings)


def test_a_lone_apprentice_with_nobody_to_pair_is_flagged_not_sent(ws):
    project = Project(name="Solo", estimated_hours=20.0, crew_size_target=1).to_dict()
    apprentice = Employee(
        name="Apprentice", crew_role=CrewRole.APPRENTICE, works_alone=False, skills={"surface_prep": 2}
    )
    proposal = assemble_crew(project, [(apprentice, 0)], crew_size=1, on=MONDAY)
    assert len(proposal.crew) == 1
    assert any("alone on site" in w and "deferring" in w for w in proposal.warnings)


def test_no_eligible_crew_is_low_confidence_not_a_guess(ws):
    project = Project(name="Impossible", estimated_hours=40.0, required_certifications=["confined_space"]).to_dict()
    proposal = assemble_crew(project, [(Employee(name="Nobody"), 0)], on=MONDAY)
    assert proposal.crew == []
    assert proposal.confidence == "low"


# --- hard constraints ------------------------------------------------------


def test_double_booking_is_blocked_with_no_approval_path(ws):
    first = make_project(ws, "First")
    second = make_project(ws, "Second")
    employee = make_employee(ws, "Booked")

    ws.ops.upsert_assignment(
        Assignment(project_id=first, employee_id=employee, work_date=MONDAY.isoformat()).to_dict()
    )
    check = check_assignment(ws, second, employee, MONDAY)
    assert check.blocks and not check.exceptions
    assert "Double-booking" in check.blocks[0]


def test_approved_time_off_is_an_exception_not_a_block(ws):
    """Management may override it; the agent may not."""
    project = make_project(ws)
    employee = make_employee(ws, "On Leave")
    ws.ops.record_time_off(
        TimeOff(
            employee_id=employee,
            start_date=MONDAY.isoformat(),
            end_date=(MONDAY + timedelta(days=4)).isoformat(),
            kind=TimeOffKind.VACATION,
            status=TimeOffStatus.APPROVED,
        ).to_dict()
    )
    check = check_assignment(ws, project, employee, MONDAY)
    assert not check.blocks
    assert check.needs_approval is True
    assert any("approved vacation" in e for e in check.exceptions)


def test_requested_time_off_only_warns(ws):
    project = make_project(ws)
    employee = make_employee(ws, "Maybe Off")
    ws.ops.record_time_off(
        TimeOff(
            employee_id=employee,
            start_date=MONDAY.isoformat(),
            end_date=MONDAY.isoformat(),
            status=TimeOffStatus.REQUESTED,
        ).to_dict()
    )
    check = check_assignment(ws, project, employee, MONDAY)
    assert check.allowed is True
    assert any("not yet decided" in w for w in check.warnings)


def test_daily_hour_cap_is_a_hard_block(ws):
    project = make_project(ws)
    employee = make_employee(ws, "Long Day")
    ws.ops.upsert_assignment(
        Assignment(project_id=project, employee_id=employee, work_date=MONDAY.isoformat(), hours=10.0).to_dict()
    )
    # Same project, so the double-booking rule is not what fires here.
    check = check_assignment(ws, project, employee, MONDAY, hours=6.0)
    assert any(f"{MAX_HOURS_PER_DAY:.0f}h daily safety cap" in b for b in check.blocks)


def test_overtime_threshold_needs_approval_but_the_safety_cap_does_not_bend(ws):
    project = make_project(ws)
    employee = make_employee(ws, "Overtime")
    for offset in range(5):                       # Mon-Fri, 8h each = 40h
        ws.ops.upsert_assignment(
            Assignment(
                project_id=project,
                employee_id=employee,
                work_date=(MONDAY + timedelta(days=offset)).isoformat(),
                hours=8.0,
            ).to_dict()
        )
    saturday = MONDAY + timedelta(days=5)
    assert week_start(saturday) == MONDAY

    check = check_assignment(ws, project, employee, saturday, hours=8.0)
    assert check.needs_approval is True
    assert any("overtime threshold" in e for e in check.exceptions)

    # Push the same week past the absolute cap: now there is no approval path.
    for offset in range(5):
        ws.ops.upsert_assignment(
            Assignment(
                project_id=project,
                employee_id=employee,
                work_date=(MONDAY + timedelta(days=offset)).isoformat(),
                hours=11.0,
                id=f"asg_extra_{offset}",
            ).to_dict()
        )
    capped = check_assignment(ws, project, employee, saturday, hours=8.0)
    assert any("weekly safety cap" in b for b in capped.blocks)


def test_cancelled_shifts_free_the_day(ws):
    project = make_project(ws)
    other = make_project(ws, "Other")
    employee = make_employee(ws, "Freed")
    assignment = Assignment(project_id=project, employee_id=employee, work_date=MONDAY.isoformat())
    ws.ops.upsert_assignment(assignment.to_dict())
    assert check_assignment(ws, other, employee, MONDAY).blocks

    ws.ops.cancel_assignment(assignment.id, "client moved the start")
    assert check_assignment(ws, other, employee, MONDAY).allowed is True


# --- the tool layer --------------------------------------------------------


def test_assign_crew_refuses_and_writes_nothing(ws, box):
    project = make_project(ws)
    other = make_project(ws, "Other")
    employee = make_employee(ws, "Busy")
    day = MONDAY.isoformat()

    assert box.call("assign_crew", {"project_id": project, "employee_id": employee, "work_date": day})["status"] == "draft"

    refused = box.call("assign_crew", {"project_id": other, "employee_id": employee, "work_date": day})
    assert refused["status"] == "refused"
    assert refused["reason"] == "hard constraint"
    assert ws.ops.assignments_between(MONDAY, MONDAY, project_id=other) == []


def test_a_model_supplied_approval_id_is_discarded(ws, box):
    """The gate is not negotiable: only the harness can supply an approval."""
    project = make_project(ws)
    employee = make_employee(ws, "On Leave")
    ws.ops.record_time_off(
        TimeOff(employee_id=employee, start_date=MONDAY.isoformat(), end_date=MONDAY.isoformat()).to_dict()
    )
    forged = box.call(
        "assign_crew",
        {
            "project_id": project,
            "employee_id": employee,
            "work_date": MONDAY.isoformat(),
            "approval_id": "appr_i_made_this_up",
        },
    )
    assert forged["status"] == "refused"
    assert forged["reason"] == "management approval required"

    # The same call, with the harness supplying the approval, goes through.
    allowed = box.call(
        "assign_crew",
        {"project_id": project, "employee_id": employee, "work_date": MONDAY.isoformat()},
        approval_id="appr_real",
    )
    assert allowed["status"] == "draft"
    assert allowed["assignment"]["approval_id"] == "appr_real"


def test_approval_cannot_create_a_hard_blocked_shift(ws, box):
    project = make_project(ws)
    other = make_project(ws, "Other")
    employee = make_employee(ws, "Busy")
    ws.ops.upsert_assignment(
        Assignment(project_id=project, employee_id=employee, work_date=MONDAY.isoformat()).to_dict()
    )
    result = box.call(
        "request_scheduling_exception",
        {
            "project_id": other,
            "employee_id": employee,
            "work_date": MONDAY.isoformat(),
            "justification": "we really need them",
        },
        approval_id="appr_real",
    )
    assert result["status"] == "refused"
    assert "not an approvable exception" in result["reason"]


def test_confirmation_rechecks_the_world(ws, box):
    """A draft written on Monday must not confirm blind on Wednesday."""
    project = make_project(ws)
    employee = make_employee(ws, "Later Off")
    draft = box.call(
        "assign_crew",
        {"project_id": project, "employee_id": employee, "work_date": MONDAY.isoformat()},
    )["assignment"]

    # Time off gets approved after the draft was written.
    ws.ops.record_time_off(
        TimeOff(employee_id=employee, start_date=MONDAY.isoformat(), end_date=MONDAY.isoformat()).to_dict()
    )
    result = box.call("confirm_shifts", {"assignment_ids": [draft["id"]]})
    assert result["confirmed"] == 0
    assert result["blocked_on_recheck"][0]["assignment_id"] == draft["id"]
    assert ws.ops.get_assignment(draft["id"])["status"] == AssignmentStatus.DRAFT.value


def test_crew_is_never_briefed_on_a_draft(ws, box):
    project = make_project(ws, address="1 Test Way")
    employee = make_employee(ws, "Crew", email="crew@example.com")
    box.call("assign_crew", {"project_id": project, "employee_id": employee, "work_date": MONDAY.isoformat()})

    refused = box.call(
        "send_crew_schedule",
        {"project_id": project, "work_date": MONDAY.isoformat(), "planned_work": "Prep"},
    )
    assert refused["status"] == "refused"

    box.call("confirm_shifts", {"project_id": project})
    sent = box.call(
        "send_crew_schedule",
        {"project_id": project, "work_date": MONDAY.isoformat(), "planned_work": "Prep and mask L2"},
    )
    assert sent["status"] == "sent"
    # Every field the communication rules require is present.
    for fragment in ("Address:", "Start time:", "Crew:", "Team lead:", "Planned work:",
                     "Equipment/PPE:", "Completion target:", "Reporting:", "Questions:"):
        assert fragment in sent["message"]


def test_time_entries_drive_actual_hours(ws, box):
    project = make_project(ws, labour_budget_hours=10.0)
    employee = make_employee(ws, "Worker")

    rejected = box.call(
        "record_time_entry",
        {"employee_id": employee, "work_date": MONDAY.isoformat(), "clock_in": "15:00", "clock_out": "07:00"},
    )
    assert "error" in rejected

    box.call(
        "record_time_entry",
        {
            "employee_id": employee,
            "work_date": MONDAY.isoformat(),
            "clock_in": "07:00",
            "clock_out": "15:30",
            "project_id": project,
        },
    )
    assert ws.ops.get_project(project)["actual_hours"] == 8.5


def test_equipment_cannot_be_booked_twice(ws, box):
    first = make_project(ws, "First")
    second = make_project(ws, "Second")
    ws.ops.upsert_equipment({"id": "eqp_test", "name": "Sprayer", "kind": "sprayer", "active": True})

    assert box.call("book_equipment", {
        "equipment_id": "eqp_test", "project_id": first,
        "start_date": MONDAY.isoformat(), "end_date": (MONDAY + timedelta(days=3)).isoformat(),
    })["status"] == "booked"

    clash = box.call("book_equipment", {
        "equipment_id": "eqp_test", "project_id": second,
        "start_date": (MONDAY + timedelta(days=2)).isoformat(),
        "end_date": (MONDAY + timedelta(days=5)).isoformat(),
    })
    assert clash["status"] == "refused"


def test_client_message_has_no_free_text_channel(ws, box):
    """Structured fields only — internal cost and employee data cannot ride along."""
    project = make_project(ws, client_email="client@example.com")
    result = box.call("notify_client_schedule_change", {
        "project_id": project,
        "change": "Exterior work moves to the week of the 24th",
        "reason": "Forecast rain through the current window",
        "impact": "Completion date unchanged",
        "next_step": "Confirm lift access for the 24th",
    })
    assert result["status"] == "sent"
    body = result["message"]
    assert "Schedule change:" in body and "Expected impact:" in body
    # There is no parameter through which arbitrary text could be added.
    import inspect
    from lumia.scheduling_tools import SchedulingTools
    params = set(inspect.signature(SchedulingTools(ws)._notify_client_schedule_change).parameters)
    assert params == {
        "project_id", "change", "reason", "impact", "next_step", "to",
        "coordination_required", "approval_id",
    }


# --- planning --------------------------------------------------------------


def test_plan_never_books_one_person_twice_in_a_day(ws):
    make_project(ws, "Alpha", priority=ProjectPriority.CONTRACT_DEADLINE, estimated_hours=400.0)
    make_project(ws, "Beta", priority=ProjectPriority.CLIENT_COMMITMENT, estimated_hours=400.0)
    for index in range(4):
        make_employee(ws, f"Painter {index}")

    plan = plan_week(ws, MONDAY, days=5)
    seen: set[tuple[str, str]] = set()
    for shift in plan["shifts"]:
        for member in shift["crew"]:
            key = (member["employee_id"], shift["date"])
            assert key not in seen, "the same person was planned onto two projects on one day"
            seen.add(key)


def test_plan_resets_the_overtime_ceiling_at_each_week_boundary(ws):
    """Regression: weekly hours used to accumulate across the whole window,
    so nobody could be scheduled in the second week of a longer plan."""
    make_project(ws, "Long", estimated_hours=2000.0, crew_size_target=1)
    make_employee(ws, "Only Painter")

    plan = plan_week(ws, MONDAY, days=10)
    weeks = {week_start(date.fromisoformat(s["date"])).isoformat() for s in plan["shifts"]}
    assert len(weeks) == 2, "the second week should still be schedulable"


def test_plan_respects_approved_time_off(ws):
    make_project(ws, "Staffed", estimated_hours=400.0, crew_size_target=1)
    employee = make_employee(ws, "Away")
    ws.ops.record_time_off(
        TimeOff(
            employee_id=employee,
            start_date=MONDAY.isoformat(),
            end_date=(MONDAY + timedelta(days=2)).isoformat(),
        ).to_dict()
    )
    plan = plan_week(ws, MONDAY, days=5)
    booked = {s["date"] for s in plan["shifts"]}
    assert MONDAY.isoformat() not in booked
    assert (MONDAY + timedelta(days=3)).isoformat() in booked


def test_plan_is_labelled_provisional(ws):
    make_project(ws, "Anything", estimated_hours=80.0)
    make_employee(ws, "Someone")
    plan = plan_week(ws, MONDAY, days=3)
    assert plan["status"] == "provisional"
    assert "PROVISIONAL" in plan["instruction"]


def test_projects_rank_in_the_spec_priority_order(ws):
    make_project(ws, "Tentative", priority=ProjectPriority.TENTATIVE, status=ProjectStatus.TENTATIVE)
    make_project(ws, "Safety", priority=ProjectPriority.SAFETY)
    make_project(ws, "Deadline", priority=ProjectPriority.CONTRACT_DEADLINE)
    assert [p["name"] for p in rank_projects(ws, MONDAY)] == ["Safety", "Deadline", "Tentative"]


def test_availability_explains_why_someone_is_not_free(ws):
    project = make_project(ws)
    busy = make_employee(ws, "Busy")
    off = make_employee(ws, "Off")
    make_employee(ws, "Free")

    ws.ops.upsert_assignment(
        Assignment(project_id=project, employee_id=busy, work_date=MONDAY.isoformat()).to_dict()
    )
    ws.ops.record_time_off(
        TimeOff(employee_id=off, start_date=MONDAY.isoformat(), end_date=MONDAY.isoformat()).to_dict()
    )

    result = availability(ws, MONDAY)
    assert [e["name"] for e in result["available"]] == ["Free"]
    reasons = {e["name"]: e["reasons"] for e in result["unavailable"]}
    assert "already on" in reasons["Busy"][0]
    assert "approved" in reasons["Off"][0]


# --- monitoring ------------------------------------------------------------


def test_risk_report_catches_a_shift_on_approved_time_off(ws):
    project = make_project(ws)
    employee = make_employee(ws, "Should Be Off")
    tomorrow = date.today() + timedelta(days=1)
    ws.ops.upsert_assignment(
        Assignment(project_id=project, employee_id=employee, work_date=tomorrow.isoformat()).to_dict()
    )
    ws.ops.record_time_off(
        TimeOff(employee_id=employee, start_date=tomorrow.isoformat(), end_date=tomorrow.isoformat()).to_dict()
    )
    report = scheduling_risks(ws)
    critical = [a for a in report["alerts"] if a["severity"] == "critical"]
    assert any("approved" in a["what_happened"] for a in critical)


def test_risk_report_is_ranked_and_carries_the_required_fields(ws):
    make_project(ws, "Starting Soon", start_date=(date.today() + timedelta(days=1)).isoformat())
    make_employee(ws, "Lapsed", certifications={"boom_lift": (date.today() - timedelta(days=5)).isoformat()})

    report = scheduling_risks(ws)
    severities = [a["severity"] for a in report["alerts"]]
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    assert severities == sorted(severities, key=lambda s: order[s])
    for alert in report["alerts"]:
        for field in ("what_happened", "why_it_matters", "affected", "expected_impact", "recommended_action"):
            assert alert[field], f"{field} missing from a {alert['severity']} alert"


def test_unconnected_sources_report_unknown_not_clean(ws):
    report = scheduling_risks(ws)
    assert "UNKNOWN" in report["data_sources"]["timeclock"]
    assert "UNKNOWN" in report["data_sources"]["weather"]


def test_exterior_weather_is_unknown_when_not_connected(ws, box):
    project = make_project(ws, "Exterior", is_exterior=True, city="Calgary")
    result = box.call("check_exterior_weather", {"project_id": project})
    assert result["risk"] == "unknown"
    assert result["_mocked"] is True


# --- wiring ----------------------------------------------------------------


def test_every_registered_tool_is_classified(ws, box):
    """An unclassified tool silently gates at Level 3 and breaks its cycle."""
    from lumia.autonomy import TOOL_LEVELS

    assert [name for name in box.names() if name not in TOOL_LEVELS] == []


def test_scheduler_holds_no_outreach_tools(ws):
    from lumia.agents import ROLE_TOOLS

    forbidden = {"send_first_contact_email", "send_followup_email", "publish_content", "advance_stage"}
    assert forbidden.isdisjoint(ROLE_TOOLS["scheduler"])


def test_scheduling_requests_route_to_the_scheduler(ws):
    from lumia.llm import ClaudeClient
    from lumia.orchestrator import Orchestrator

    orchestrator = Orchestrator(workspace=ws, toolbox=Toolbox(ws), client=ClaudeClient(ws.settings))
    assert orchestrator.route("who is on the Riverbend crew tomorrow") == "scheduler"
    assert orchestrator.route("staff the Meadowlark exterior repaint next week") == "scheduler"
    # Growth requests are unaffected.
    assert orchestrator.route("draft a case study from the Riverbend job") == "content"


def test_the_gate_intercepts_a_scheduling_exception_in_a_real_run(ws):
    """End to end: the loop queues the exception and tells the model it did not happen."""
    from conftest import FakeClient, calls_tool, text

    from lumia.agent import Agent

    project = make_project(ws)
    employee = make_employee(ws, "On Leave")
    ws.ops.record_time_off(
        TimeOff(employee_id=employee, start_date=MONDAY.isoformat(), end_date=MONDAY.isoformat()).to_dict()
    )

    agent = Agent(
        role="scheduler",
        workspace=ws,
        toolbox=Toolbox(ws),
        client=FakeClient(
            [
                calls_tool(
                    "request_scheduling_exception",
                    {
                        "project_id": project,
                        "employee_id": employee,
                        "work_date": MONDAY.isoformat(),
                        "justification": "deadline recovery",
                    },
                ),
                text("Queued for approval — the shift was not created."),
            ]
        ),
    )
    run = agent.run("put someone on Monday even though they are off")

    assert len(run.approvals_raised) == 1
    assert run.tool_calls[0].executed is False
    assert ws.ops.assignments_between(MONDAY, MONDAY) == []


def test_level_two_scheduling_actions_run_without_a_human(ws):
    from conftest import FakeClient, calls_tool, text

    from lumia.agent import Agent

    project = make_project(ws)
    employee = make_employee(ws, "Available")
    agent = Agent(
        role="scheduler",
        workspace=ws,
        toolbox=Toolbox(ws),
        client=FakeClient(
            [
                calls_tool(
                    "assign_crew",
                    {"project_id": project, "employee_id": employee, "work_date": MONDAY.isoformat()},
                ),
                text("Drafted."),
            ]
        ),
    )
    run = agent.run("staff Monday")

    assert run.tool_calls[0].executed is True
    assert run.approvals_raised == []
    assert len(ws.ops.assignments_between(MONDAY, MONDAY)) == 1


def test_scheduler_runs_on_the_scheduling_spec(ws):
    from lumia.prompts import system_prompt

    scheduler = system_prompt("scheduler", ws.settings)
    assert "Intelligent Scheduling Agent" in scheduler
    assert "Lumia Marketing" not in scheduler
    assert "Lumia Marketing" in system_prompt("director", ws.settings)
