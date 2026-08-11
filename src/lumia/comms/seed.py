"""Demo project data, so a fresh clone has a jobsite to communicate about.

Every record is tagged `source: "demo_seed"` and named `(DEMO)`, for the
same reason the growth seed is: the agent is under a hard rule never to
invent projects, people or work, so fixture data has to announce itself.

The seed deliberately includes an *imperfect* day — a submission in Arabic
with hours that do not match the clock records, and a photo that must not
go to a client. A demo where everything reconciles would show none of the
behaviour that matters.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..domain.projects import (
    CommChannel,
    CrewMember,
    FieldSubmission,
    MediaAsset,
    MediaFlag,
    Project,
    ProjectContact,
    ProjectStatus,
    RecipientRole,
    SubmissionKind,
    TimeRecord,
)
from ..workspace import Workspace

SEED_TAG = "demo_seed"


def _today() -> str:
    return date.today().isoformat()


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def seed_demo_projects(ws: Workspace) -> dict[str, Any]:
    """Idempotent: re-running replaces the demo records rather than duplicating."""
    for project in [p for p in ws.comms.list_projects() if p.get("source") == SEED_TAG]:
        for collection, records in (
            ("project_contacts", ws.comms.contacts_for(project["id"])),
            ("submissions", ws.comms.submissions_for(project["id"])),
            ("media", ws.comms.media_for(project["id"])),
            ("time_records", ws.comms.time_records_for(project["id"])),
            ("daily_logs", ws.comms.daily_logs_for(project["id"])),
            ("communications", ws.comms.communications_for(project["id"])),
            ("open_items", ws.comms.open_items(project["id"], include_closed=True)),
            ("escalations", ws.comms.escalations(project["id"])),
        ):
            for record in records:
                ws.store.delete(collection, record["id"])
        ws.store.delete("projects", project["id"])

    project = Project(
        name="Riverbend Medical Centre – Phase 2 (DEMO)",
        number="AP-2026-118",
        address="4400 Riverbend Way SE, Calgary, AB",
        client_name="Northgate Construction Group (DEMO)",
        general_contractor="Northgate Construction Group (DEMO)",
        scope_summary=(
            "Interior repaint of levels 2 and 3: corridors, patient rooms, and staff areas. "
            "Prime and two finish coats to walls; existing doors and frames refinished."
        ),
        exclusions=[
            "Ceiling painting is excluded from the contract.",
            "Epoxy floor coatings are excluded and priced separately.",
        ],
        status=ProjectStatus.ACTIVE,
        site_access_notes="Occupied facility. Work in corridors after 18:00 only. Sign in at Level 1 security.",
        preferred_channel=CommChannel.EMAIL,
        daily_log_time="17:00",
        report_length="standard",
        source=SEED_TAG,
    )
    ws.comms.upsert_project(project.to_dict())

    contacts = [
        ProjectContact(
            project_id=project.id,
            name="Dana Whitfield (DEMO)",
            role=RecipientRole.PROJECT_MANAGER,
            company="Northgate Construction Group (DEMO)",
            email="dana.whitfield@example.com",
            phone="+15550100",
            preferred_channel=CommChannel.EMAIL,
            receives_daily_log=True,
            notes="Wants the daily log by 17:00. Prefers issues called out at the top.",
        ),
        ProjectContact(
            project_id=project.id,
            name="Marcus Reyes (DEMO)",
            role=RecipientRole.SUPERINTENDENT,
            company="Northgate Construction Group (DEMO)",
            email="marcus.reyes@example.com",
            phone="+15550101",
            preferred_channel=CommChannel.SMS,
            receives_daily_log=True,
            notes="On site daily. Texts back quickly; email goes unread for days.",
        ),
        ProjectContact(
            project_id=project.id,
            name="Priya Anand (DEMO)",
            role=RecipientRole.SUPPLIER,
            company="Westline Coatings Supply (DEMO)",
            email="orders@example.com",
            phone="+15550102",
            preferred_channel=CommChannel.EMAIL,
            notes="Account 8841. Deliveries to the Level 1 loading bay before 15:00.",
        ),
    ]
    for contact in contacts:
        ws.comms.upsert_contact(contact.to_dict())

    crew = [
        CrewMember(
            name="Yusuf Haddad (DEMO)",
            role="crew_lead",
            phone="+15550110",
            preferred_language="ar",
            project_ids=[project.id],
            notes="Reports by voice message in Arabic at the end of each shift.",
        ),
        CrewMember(
            name="Tomas Lindqvist (DEMO)",
            role="painter",
            phone="+15550111",
            preferred_language="en",
            project_ids=[project.id],
        ),
    ]
    for member in crew:
        ws.comms.upsert_crew(member.to_dict())

    # Clock records: two people, 8 and 7.5 hours. The submission below will
    # report 20, which is the point.
    for member, clock_in, clock_out, hours in (
        (crew[0], "18:00", "02:00", 8.0),
        (crew[1], "18:00", "01:30", 7.5),
    ):
        ws.comms.record_time(
            TimeRecord(
                project_id=project.id,
                crew_id=member.id,
                crew_name=member.name,
                work_date=_today(),
                clock_in=clock_in,
                clock_out=clock_out,
                hours=hours,
            ).to_dict()
        )

    submission = FieldSubmission(
        project_id=project.id,
        submitted_by=crew[0].id,
        submitted_by_name=crew[0].name,
        kind=SubmissionKind.VOICE,
        original_language="ar",
        original_text="",
        transcript=(
            "خلصنا التحضير في الممر الشمالي وحطينا البرايمر. غرفة ٢٠٤ صارت جاهزة بس في "
            "رطوبة على الحيط عند الشباك. اشتغلنا ٢٠ ساعة اليوم. السقف كان محتاج دهان بعد."
        ),
        hours_reported=20.0,
        crew_count_reported=2,
        work_areas=["North corridor", "Room 204"],
        activities=["surface preparation", "primer application"],
        work_date=_today(),
    )
    ws.comms.record_submission(submission.to_dict())

    media = [
        MediaAsset(
            project_id=project.id,
            submission_id=submission.id,
            uri="https://example.invalid/demo/north-corridor-primer.jpg",
            work_area="North corridor",
            taken_on=_today(),
            sequence=1,
        ),
        MediaAsset(
            project_id=project.id,
            submission_id=submission.id,
            uri="https://example.invalid/demo/room-204-moisture.jpg",
            work_area="Room 204",
            taken_on=_today(),
            sequence=2,
        ),
        MediaAsset(
            project_id=project.id,
            submission_id=submission.id,
            uri="https://example.invalid/demo/level-1-security-panel.jpg",
            work_area="Level 1 entry",
            taken_on=_today(),
            caption="Security keypad at the Level 1 entrance.",
            flags=[MediaFlag.SECURITY],
            sequence=3,
        ),
    ]
    for asset in media:
        ws.comms.add_media(asset.to_dict())

    return {
        "project_id": project.id,
        "project": project.name,
        "contacts": len(contacts),
        "crew": len(crew),
        "submissions": 1,
        "media": len(media),
        "note": (
            "Seeded with an Arabic voice report whose hours contradict the clock records, "
            "and a photo of a security keypad that must never reach a client. Run "
            "`lumia comms intake` to see the cross-check and the media flag do their work."
        ),
        "cleanup": "Delete data/ before pointing this at a real project.",
    }
