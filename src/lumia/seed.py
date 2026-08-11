"""Demo data so a fresh clone has something to operate on.

Every record is explicitly marked `source: "demo_seed"` and uses obviously
fictional names. This matters: Lumia is under a hard rule never to invent
accounts, contacts or relationships, so fixture data must be
self-identifying and easy to purge before real use.

    lumia seed          # load it
    rm -rf data/        # remove it
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .domain.accounts import (
    Account,
    AccountTier,
    AccountType,
    Channel,
    Confidence,
    Contact,
    Interaction,
    MarketSignal,
    Opportunity,
    PipelineStage,
)
from .memory import Lesson
from .workspace import Workspace

SEED_TAG = "demo_seed"


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _in_days(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


def seed_demo_data(ws: Workspace) -> dict[str, Any]:
    """Idempotent: re-running replaces the demo records rather than duplicating."""
    existing = [a for a in ws.crm.list_accounts() if a.get("source") == SEED_TAG]
    for account in existing:
        ws.store.delete("accounts", account["id"])

    created: list[str] = []

    # --- Tier A: a GC with an active relationship -----------------------
    gc = Account(
        name="Northgate Construction Group (DEMO)",
        account_type=AccountType.GENERAL_CONTRACTOR,
        website="https://example.com/northgate",
        location="Calgary, AB",
        size_note="~35 staff, mid-market commercial GC",
        tier=AccountTier.A,
        stage=PipelineStage.ENGAGED,
        pain_points=["Subs missing turnover dates", "Slow bid responses from painters"],
        likely_needs=["Tenant improvement painting", "New construction painting"],
        estimated_annual_value=180_000,
        next_action="Send Q3 availability and confirm we're on the bid list for the Elbow River fit-out",
        next_action_date=_in_days(3),
        notes="Fictional account for demonstration.",
        source=SEED_TAG,
    )
    ws.crm.upsert_account(gc.to_dict())
    created.append(gc.name)

    estimator = Contact(
        account_id=gc.id,
        name="Dana Whitfield (DEMO)",
        title="Senior Estimator",
        is_decision_maker=True,
        notes="Fictional contact. Prefers short emails with the number up front.",
    )
    ws.crm.upsert_contact(estimator.to_dict())

    for offset, (summary, outcome) in enumerate(
        [
            ("Invited to bid on the Riverbend office fit-out", "tender_invitation"),
            ("Submitted budgetary pricing for Riverbend", "sent"),
            ("Asked about crew availability for August", "positive_reply"),
        ]
    ):
        ws.crm.log_interaction(
            Interaction(
                account_id=gc.id,
                channel=Channel.EMAIL,
                direction="inbound" if outcome != "sent" else "outbound",
                summary=summary,
                contact_id=estimator.id,
                outcome=outcome,
                occurred_on=_days_ago(30 - offset * 10),
            ).to_dict()
        )

    ws.crm.upsert_opportunity(
        Opportunity(
            account_id=gc.id,
            description="Riverbend office fit-out — 22,000 sqft interior repaint",
            estimated_value=64_000,
            stage=PipelineStage.ESTIMATE_SUBMITTED,
            probability=0.45,
            expected_decision_date=_in_days(21),
        ).to_dict()
    )

    # --- Tier B: property management, gone quiet ------------------------
    pm = Account(
        name="Bowmont Property Services (DEMO)",
        account_type=AccountType.PROPERTY_MANAGEMENT,
        website="https://example.com/bowmont",
        location="Calgary, AB",
        size_note="~40 mixed-use properties",
        tier=AccountTier.B,
        stage=PipelineStage.CONTACTED,
        pain_points=["Suite turnover downtime", "Inconsistent painting quality between vendors"],
        likely_needs=["Suite turnovers", "Common-area repaint", "Parkade line painting"],
        estimated_annual_value=75_000,
        next_action="",  # deliberately blank — demonstrates the unmanaged-account flag
        notes="Fictional account for demonstration.",
        source=SEED_TAG,
    )
    ws.crm.upsert_account(pm.to_dict())
    created.append(pm.name)

    ws.crm.upsert_contact(
        Contact(
            account_id=pm.id,
            name="Marcus Reyes (DEMO)",
            title="Regional Property Manager",
            is_decision_maker=True,
            notes="Fictional contact.",
        ).to_dict()
    )
    ws.crm.log_interaction(
        Interaction(
            account_id=pm.id,
            channel=Channel.EMAIL,
            direction="outbound",
            summary="Introduction email about suite turnover programs",
            outcome="no_response",
            occurred_on=_days_ago(52),
        ).to_dict()
    )

    # --- Tier C: cold institutional prospect ----------------------------
    inst = Account(
        name="Crescent Valley School Division (DEMO)",
        account_type=AccountType.INSTITUTION,
        location="Airdrie, AB",
        size_note="12 schools",
        tier=AccountTier.C,
        stage=PipelineStage.PROSPECT,
        likely_needs=["Summer break repaints", "Gymnasium coatings"],
        estimated_annual_value=45_000,
        next_action="Research procurement process and vendor registration requirements",
        next_action_date=_in_days(10),
        notes="Fictional account for demonstration. No prior contact — genuinely cold.",
        source=SEED_TAG,
    )
    ws.crm.upsert_account(inst.to_dict())
    created.append(inst.name)

    # --- A signal that already carries an action ------------------------
    ws.crm.record_signal(
        MarketSignal(
            headline="Permit issued for 18,000 sqft retail fit-out at Elbow River Centre (DEMO)",
            signal_type="permit",
            source="demo_seed",
            confidence=Confidence.INFERENCE,
            likely_account="Northgate Construction Group (DEMO)",
            who_controls_it="Northgate is listed as general contractor",
            painting_likely=True,
            likely_timing="Painting scope likely 8-10 weeks out",
            recommended_action=(
                "Email Dana Whitfield referencing the Riverbend submission and ask to be "
                "included on the Elbow River bid list. Send within 3 days."
            ),
        ).to_dict()
    )

    # --- One seeded lesson, honestly labelled as low strength -----------
    ws.memory.record_lesson(
        Lesson(
            observation="Property manager outreach framed around turnover downtime got a reply; "
            "outreach framed around paint quality did not.",
            hypothesis="Property managers weigh vacancy days more heavily than finish quality.",
            evidence="Single A/B pair in the demo dataset — not yet real evidence.",
            confidence=Confidence.INFERENCE,
            segment=AccountType.PROPERTY_MANAGEMENT.value,
            channel=Channel.EMAIL.value,
            sample_size=1,
            recommended_use="Worth testing properly before treating as a rule.",
        )
    )

    operations = seed_operations_data(ws)

    return {
        "seeded_accounts": created,
        "seeded_operations": operations,
        "note": (
            "Demo records are tagged source='demo_seed' and named (DEMO). "
            "Delete the data/ directory before using this against real accounts."
        ),
        "pipeline": ws.crm.pipeline(),
    }


# --- delivery side: crew, projects and shifts -----------------------------
#
# Fixed ids keep this idempotent and make the demo trivial to purge. The
# dataset deliberately contains live problems — an expiring certification, an
# approved vacation across a busy week, an exterior job against a tight
# deadline — because a seed where nothing is wrong cannot demonstrate a risk
# report.

DEMO_EMPLOYEE_IDS = ("emp_demo_lead", "emp_demo_spray", "emp_demo_fine", "emp_demo_appr", "emp_demo_ext")
DEMO_PROJECT_IDS = ("proj_demo_riverbend", "proj_demo_meadowlark", "proj_demo_clinic")


def seed_operations_data(ws: Workspace) -> dict[str, Any]:
    from .domain.projects import Project, ProjectPriority, ProjectStatus, phase_plan
    from .domain.workforce import CrewRole, Employee, Equipment, TimeOff, TimeOffKind, TimeOffStatus

    for collection, ids in (
        ("employees", DEMO_EMPLOYEE_IDS),
        ("projects", DEMO_PROJECT_IDS),
    ):
        for record_id in ids:
            ws.store.delete(collection, record_id)
    for collection in ("project_phases", "assignments", "time_off", "equipment", "equipment_bookings"):
        for record in ws.store.list(collection):
            if str(record.get("id", "")).endswith("_demo") or str(record.get("id", "")).startswith(
                ("phs_demo", "asg_demo", "tmo_demo", "eqp_demo", "eqb_demo")
            ):
                ws.store.delete(collection, record["id"])

    crew = [
        Employee(
            id="emp_demo_lead",
            name="Maya Okonkwo (DEMO)",
            crew_role=CrewRole.LEAD,
            email="maya@example.com",
            skills={
                "surface_prep": 4, "cut_and_roll": 4, "spray": 3, "back_roll": 3,
                "fine_finish": 4, "drywall_repair": 3, "client_facing": 4,
            },
            certifications={
                "working_at_heights": _in_days(300), "whmis": "", "first_aid": _in_days(210),
                "boom_lift": _in_days(400),
            },
            productivity_factor=1.15,
            reliability=0.98,
            can_lead=True,
            home_base="Calgary, AB",
        ),
        Employee(
            id="emp_demo_spray",
            name="Tomás Bergeron (DEMO)",
            crew_role=CrewRole.SENIOR_PAINTER,
            email="tomas@example.com",
            skills={"spray": 4, "back_roll": 4, "surface_prep": 3, "epoxy_floor": 3, "exterior": 3},
            # Expiring soon on purpose: this is what the risk report catches.
            certifications={"working_at_heights": _in_days(21), "scissor_lift": _in_days(180), "whmis": ""},
            productivity_factor=1.2,
            reliability=0.94,
            can_lead=True,
            home_base="Calgary, AB",
        ),
        Employee(
            id="emp_demo_fine",
            name="Priya Raman (DEMO)",
            crew_role=CrewRole.PAINTER,
            email="priya@example.com",
            skills={"fine_finish": 4, "cut_and_roll": 4, "wood_finishing": 3, "colour_matching": 3, "surface_prep": 3},
            certifications={"whmis": "", "first_aid": _in_days(90)},
            productivity_factor=1.05,
            reliability=0.99,
            home_base="Calgary, AB",
        ),
        Employee(
            id="emp_demo_appr",
            name="Devin Cross (DEMO)",
            crew_role=CrewRole.APPRENTICE,
            email="devin@example.com",
            skills={"surface_prep": 2, "cut_and_roll": 1, "spray": 1},
            certifications={"whmis": ""},
            productivity_factor=0.75,
            reliability=0.9,
            works_alone=False,
            development_notes="Building cut-and-roll speed; pair with a finisher on trim work.",
            home_base="Calgary, AB",
        ),
        Employee(
            id="emp_demo_ext",
            name="Jordan Alvi (DEMO)",
            crew_role=CrewRole.PAINTER,
            email="jordan@example.com",
            skills={"exterior": 4, "spray": 3, "surface_prep": 3, "back_roll": 3},
            # Already lapsed: shows how a blocker differs from a warning.
            certifications={"boom_lift": _days_ago(12), "working_at_heights": _in_days(120), "whmis": ""},
            productivity_factor=1.0,
            reliability=0.92,
            home_base="Airdrie, AB",
        ),
    ]
    for employee in crew:
        ws.ops.upsert_employee(employee.to_dict())

    gc_matches = ws.crm.find_accounts("Northgate")
    gc_account_id = gc_matches[0]["id"] if gc_matches else ""

    projects = [
        Project(
            id="proj_demo_riverbend",
            name="Riverbend Tower L3 Tenant Improvement (DEMO)",
            address="120 Riverbend Way SE, Calgary",
            city="Calgary",
            client_name="Northgate Construction Group (DEMO)",
            client_email="dana@example.com",
            account_id=gc_account_id,
            status=ProjectStatus.ACTIVE,
            priority=ProjectPriority.CONTRACT_DEADLINE,
            start_date=_days_ago(4),
            deadline=_in_days(12),
            estimated_hours=320.0,
            labour_budget_hours=340.0,
            actual_hours=96.0,
            crew_size_target=3,
            required_skills=["surface_prep", "cut_and_roll", "fine_finish"],
            required_certifications=["whmis"],
            site_access_start="07:00",
            site_access_end="17:00",
            access_notes="Loading bay off Riverbend Way. Book the freight elevator with site super.",
            blocking_trade="Drywall finishers — L3 north wing",
            scope_notes="Open-plan office, 11ft ceilings. Spray ceilings before partitions go in.",
        ),
        Project(
            id="proj_demo_meadowlark",
            name="Meadowlark Plaza Exterior Repaint (DEMO)",
            address="8800 Meadowlark Rd NW, Calgary",
            city="Calgary",
            client_name="Beacon Property Partners (DEMO)",
            client_email="ops@example.com",
            status=ProjectStatus.CONFIRMED,
            priority=ProjectPriority.CLIENT_COMMITMENT,
            start_date=_in_days(3),
            deadline=_in_days(24),
            estimated_hours=260.0,
            labour_budget_hours=260.0,
            crew_size_target=3,
            required_skills=["exterior", "spray", "surface_prep"],
            required_certifications=["working_at_heights", "boom_lift"],
            is_exterior=True,
            site_access_start="07:00",
            site_access_end="19:00",
            access_notes="Boom lift staged on the north lot. Tenants park the south lot until 18:00.",
            scope_notes="Two-storey retail strip. Elastomeric over stucco, two coats.",
        ),
        Project(
            id="proj_demo_clinic",
            name="Willowbrook Clinic Repaint (DEMO)",
            address="45 Willowbrook Dr NE, Calgary",
            city="Calgary",
            client_name="Willowbrook Health (DEMO)",
            client_email="facilities@example.com",
            status=ProjectStatus.TENTATIVE,
            priority=ProjectPriority.TENTATIVE,
            start_date=_in_days(18),
            deadline=_in_days(40),
            estimated_hours=140.0,
            labour_budget_hours=150.0,
            crew_size_target=2,
            required_skills=["fine_finish", "cut_and_roll", "client_facing"],
            required_certifications=["whmis"],
            is_occupied=True,
            site_access_start="18:00",
            site_access_end="23:00",
            access_notes="After-hours only. Security badge from reception; clinic reopens 07:00.",
            scope_notes="Occupied clinic — low-VOC only, full containment, nightly reinstatement.",
        ),
    ]
    for project in projects:
        ws.ops.upsert_project(project.to_dict())
        ws.ops.put_phases([p.to_dict() for p in phase_plan(project)])

    # Riverbend is part-built: prep done, masking under way.
    for phase_name, pct in (("mobilization", 100.0), ("inspection", 100.0), ("prep", 100.0), ("masking", 45.0)):
        for phase in ws.ops.phases_for("proj_demo_riverbend"):
            if phase.get("phase") == phase_name:
                ws.ops.patch_phase(
                    str(phase["id"]),
                    {"completed_pct": pct, "status": "complete" if pct >= 100 else "in_progress"},
                )

    ws.ops.record_time_off(
        TimeOff(
            id="tmo_demo_priya",
            employee_id="emp_demo_fine",
            start_date=_in_days(4),
            end_date=_in_days(8),
            kind=TimeOffKind.VACATION,
            status=TimeOffStatus.APPROVED,
            note="Booked in March.",
        ).to_dict()
    )

    for equipment in (
        Equipment(id="eqp_demo_sprayer", name="Graco airless sprayer #2 (DEMO)", kind="sprayer"),
        Equipment(id="eqp_demo_lift", name="Boom lift 45ft (DEMO)", kind="lift"),
    ):
        ws.ops.upsert_equipment(equipment.to_dict())

    return {
        "employees": len(crew),
        "projects": len(projects),
        "deliberate_issues": [
            "Tomás Bergeron's working-at-heights certification expires within the month.",
            "Jordan Alvi's boom-lift certification has already lapsed, which blocks the "
            "Meadowlark exterior scope.",
            "Priya Raman has approved vacation across a week Riverbend needs finishing crew.",
        ],
        "note": "Run `lumia monitor --report-only` to see these surface as ranked risks.",
    }
