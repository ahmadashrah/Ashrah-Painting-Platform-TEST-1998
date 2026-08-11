"""The client-facing daily log: assembly, validation and rendering.

Three things are code rather than prose:

- **Assembly** gathers only what the platform can corroborate for one
  project on one date, and returns the gaps explicitly. The agent writes
  the sentences; it does not get to decide what counts as evidence.
- **Validation** refuses a log that claims completion without a
  verification, or that has no verified work in it at all.
- **Rendering** produces the section layout from the spec, and structurally
  cannot emit internal notes — `unverified_notes` is simply not read here.

The result is that a daily log's shape is identical every day, which is
what makes it useful to a general contractor reading forty of them.
"""

from __future__ import annotations

import re
from typing import Any

from ..domain.projects import (
    BLOCKING_MEDIA_FLAGS,
    DailyLog,
    MediaFlag,
    Verification,
)

#: Wording that keeps a forward-looking statement from reading as a promise.
HEDGES = ("planned", "anticipated", "expected", "subject to", "intend", "scheduled to", "if")

#: Absolute completion claims need a verified completion behind them.
COMPLETION_CLAIM = re.compile(
    r"\b(?:100\s?%|fully|entirely|all work)\s*(?:is\s*)?(?:complete|completed|finished|done)\b",
    flags=re.IGNORECASE,
)

CLOSING = (
    "Please let us know if you have any questions or require additional information.\n\n"
    "Kind regards,\n"
    "Ashrah Painting Ltd."
)


def gather(ws: Any, project_id: str, log_date: str) -> dict[str, Any]:
    """Collect the verified material available for one project-day.

    Returns facts and gaps, never prose. The agent turns this into the
    sentences a client reads — from this and nothing else.
    """
    project = ws.comms.get_project(project_id)
    if project is None:
        return {"error": f"no project with id {project_id}"}

    submissions = ws.comms.submissions_for(project_id, work_date=log_date)
    time_records = ws.comms.time_records_for(project_id, work_date=log_date)
    media = [m for m in ws.comms.media_for(project_id) if str(m.get("taken_on", "")) == log_date]

    eligible = [m for m in media if m.get("client_safe") and not m.get("blocked")]
    withheld = [
        {"id": m["id"], "flags": m.get("flags", []), "caption": m.get("caption", "")}
        for m in media
        if not m.get("client_safe") or m.get("blocked")
    ]

    clocked_hours = round(sum(float(t.get("hours", 0) or 0) for t in time_records), 2)
    crew_on_site = len({t.get("crew_id") for t in time_records if t.get("crew_id")})

    unprocessed = [s["id"] for s in submissions if not s.get("processed")]
    conflicted = [
        {"id": s["id"], "conflicts": s.get("conflicts", [])}
        for s in submissions
        if s.get("verification") == Verification.CONFLICTED.value
    ]

    gaps: list[str] = []
    if not submissions:
        gaps.append("No field submission has been received for this date.")
    if unprocessed:
        gaps.append(
            f"{len(unprocessed)} submission(s) still need translation and cleanup: "
            f"{', '.join(unprocessed)}."
        )
    if conflicted:
        gaps.append(
            f"{len(conflicted)} submission(s) conflict with platform records and must be "
            "resolved before this log is sent."
        )
    if not time_records:
        gaps.append("No clock-in records for this date — crew count and work period are unverified.")
    if not eligible:
        gaps.append("No client-safe photos are available for this date.")
    if withheld:
        gaps.append(f"{len(withheld)} photo(s) are flagged and withheld from client-facing use.")

    return {
        "project": {
            "id": project["id"],
            "name": project.get("name", ""),
            "number": project.get("number", ""),
            "address": project.get("address", ""),
            "scope_summary": project.get("scope_summary", ""),
            "exclusions": project.get("exclusions", []),
            "report_length": project.get("report_length", "standard"),
            "share_crew_count": project.get("share_crew_count", True),
            "share_work_period": project.get("share_work_period", True),
        },
        "log_date": log_date,
        "submissions": submissions,
        "verified_crew_count": crew_on_site,
        "verified_hours": clocked_hours,
        "work_period": _work_period(time_records),
        "eligible_photos": [
            {"id": m["id"], "caption": m.get("caption", ""), "work_area": m.get("work_area", "")}
            for m in eligible
        ],
        "withheld_photos": withheld,
        "open_items": ws.comms.open_items(project_id),
        "previous_log": ws.comms.previous_daily_log(project_id, before=log_date),
        "gaps": gaps,
        "ready": not unprocessed and not conflicted and bool(submissions),
        "guidance": (
            "Write each bullet only from the submissions above. Anything not present "
            "here is UNKNOWN — ask the field employee rather than filling the gap."
        ),
    }


def validate(log: DailyLog, *, completion_verified: bool = False) -> list[str]:
    """Blocking problems. An empty list means the log may be composed."""
    problems: list[str] = []

    if not log.completed and not log.in_progress:
        problems.append(
            "A daily log with no verified completed or in-progress work is not a report. "
            "Ask the field employee what was done before sending anything."
        )

    if not completion_verified:
        for bullet in log.completed + log.in_progress:
            if COMPLETION_CLAIM.search(bullet):
                problems.append(
                    f"Unverified completion claim: {bullet!r}. A photo of partial work does not "
                    "prove completion — either verify it or describe what was actually done."
                )

    for note in log.unverified_notes:
        if note in log.completed or note in log.in_progress or note in log.issues:
            problems.append(
                f"Unverified note {note!r} appears in a client-facing section. Internal notes "
                "stay internal."
            )

    return problems


def warnings(log: DailyLog) -> list[str]:
    """Non-blocking problems worth fixing before the log goes out."""
    notes: list[str] = []
    for bullet in log.next_day:
        if not any(hedge in bullet.lower() for hedge in HEDGES):
            notes.append(
                f"Next-day item {bullet!r} reads as a commitment. Use 'planned', 'anticipated' "
                "or 'subject to site readiness' unless the schedule is confirmed."
            )
    if not log.photo_ids:
        notes.append("No progress photos attached. Client-facing logs are stronger with them.")
    if not log.issues:
        notes.append("No issues recorded — confirm that nothing needs a client decision.")
    return notes


def render(project: dict[str, Any], log: DailyLog, captions: dict[str, str] | None = None) -> tuple[str, str]:
    """Return (subject, body) in the layout the spec defines.

    Internal fields are not parameters of this function's output by
    construction: `unverified_notes` and `gaps` are never read.
    """
    captions = captions or {}
    name = project.get("name", "Project")
    subject = f"Daily Project Update – {name} – {log.log_date}"

    header = [
        f"Project: {name}",
        f"Location: {project.get('address', '')}".rstrip(),
        f"Date: {log.log_date}",
    ]
    if project.get("share_crew_count", True) and log.crew_count:
        header.append(f"Ashrah Painting Crew: {log.crew_count} {'worker' if log.crew_count == 1 else 'workers'}")
    if project.get("share_work_period", True) and log.work_period:
        header.append(f"Work Period: {log.work_period}")

    parts = ["\n".join(line for line in header if line.strip())]

    parts.append(_section("Work Completed Today", log.completed))
    parts.append(_section("Work in Progress", log.in_progress))
    parts.append(_section("Materials and Equipment", log.materials))
    parts.append(_section("Site Conditions or Constraints", log.site_conditions))
    parts.append(_section("Issues Requiring Attention", log.issues, empty="None at this time."))
    parts.append(_section("Plan for the Next Working Day", log.next_day))
    parts.append(_section("Safety", log.safety, empty="No safety concerns were reported today."))

    if log.photo_ids:
        lines = []
        for index, media_id in enumerate(log.photo_ids, start=1):
            caption = captions.get(media_id, "").strip()
            lines.append(f"{index}. {caption}" if caption else f"{index}. (caption required)")
        parts.append("Progress Photos\n" + "\n".join(lines))

    parts.append(CLOSING)
    body = "\n\n".join(part for part in parts if part)
    return subject, body


def _section(title: str, bullets: list[str], empty: str = "") -> str:
    if not bullets:
        return f"{title}\n{empty}" if empty else ""
    lines = "\n".join(f"- {bullet.strip()}" for bullet in bullets if bullet.strip())
    return f"{title}\n{lines}" if lines else ""


def _work_period(time_records: list[dict[str, Any]]) -> str:
    """Earliest clock-in to latest clock-out, or blank when unverified."""
    starts = sorted(str(t.get("clock_in", "")) for t in time_records if t.get("clock_in"))
    ends = sorted(str(t.get("clock_out", "")) for t in time_records if t.get("clock_out"))
    if not starts or not ends:
        return ""
    return f"{starts[0]} – {ends[-1]}"


def eligible_media(media: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Photos that may appear in a client-facing message."""
    blocking = {f.value for f in BLOCKING_MEDIA_FLAGS}
    return [
        m
        for m in media
        if m.get("client_safe")
        and not (set(m.get("flags", [])) & blocking)
        and str(m.get("caption", "")).strip()
    ]


def flag_values(flags: list[str]) -> list[str]:
    """Normalize free-text flags to known MediaFlag values, dropping unknowns."""
    known = []
    for flag in flags:
        try:
            known.append(MediaFlag(str(flag).strip().lower()).value)
        except ValueError:
            continue
    return known
