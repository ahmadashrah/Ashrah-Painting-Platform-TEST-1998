"""Cross-checking a field submission against what the platform already knows.

The spec's rule is that a conflict between a worker's note and the
platform record is flagged for review rather than silently resolved. That
only works if the comparison is mechanical, so it happens here:
reported hours against clock records, reported headcount against who
actually clocked in, reported work against the project's exclusions, and
today's report against what earlier reports already claimed.

The corroborating data — clock-ins, scope, prior logs — is written by the
platform, never by the agent. Nothing on the tool surface can create a
time record, so the agent cannot manufacture agreement with itself.
"""

from __future__ import annotations

import re
from typing import Any

from ..domain.projects import LIVE_STATUSES, ProjectStatus, Verification, today_iso

#: Hours may differ this much from the clock records before it is a conflict —
#: rounding and a late clock-out are not contradictions.
HOURS_TOLERANCE = 1.0

#: Words too generic to identify a scope exclusion.
GENERIC = {
    "paint", "painting", "painted", "work", "works", "excluded", "exclusion",
    "excludes", "scope", "area", "areas", "all", "any", "and", "the", "for",
    "from", "with", "this", "that", "not", "included", "including", "site",
    "project", "coat", "coats", "apply", "application",
}


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in GENERIC}


def verify(
    submission: dict[str, Any],
    *,
    project: dict[str, Any] | None,
    time_records: list[dict[str, Any]],
    prior_submissions: list[dict[str, Any]],
    prior_logs: list[dict[str, Any]],
    media: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the verification state, conflicts and missing information."""
    conflicts: list[str] = []
    missing: list[str] = []

    if project is None:
        return {
            "verification": Verification.CONFLICTED.value,
            "conflicts": [f"No project with id {submission.get('project_id')} exists."],
            "missing": [],
        }

    status = str(project.get("status", ""))
    if status not in {s.value for s in LIVE_STATUSES}:
        conflicts.append(
            f"Work was reported against a project whose status is '{status}', not "
            f"{' or '.join(sorted(s.value for s in LIVE_STATUSES))}."
        )
        if status == ProjectStatus.ON_HOLD.value:
            conflicts.append("The project is on hold — confirm the crew was authorized to work.")

    work_date = str(submission.get("work_date", ""))
    if work_date and work_date > today_iso():
        conflicts.append(f"The reported work date {work_date} is in the future.")

    # --- hours and headcount against the clock ---------------------------

    clocked_hours = round(sum(float(t.get("hours", 0) or 0) for t in time_records), 2)
    clocked_crew = len({t.get("crew_id") for t in time_records if t.get("crew_id")})
    reported_hours = float(submission.get("hours_reported", 0) or 0)
    reported_crew = int(submission.get("crew_count_reported", 0) or 0)

    if not time_records:
        missing.append("No clock-in records exist for this project and date.")
    else:
        if reported_hours and abs(reported_hours - clocked_hours) > HOURS_TOLERANCE:
            conflicts.append(
                f"Reported {reported_hours} labour hours but clock records show "
                f"{clocked_hours}. Confirm which is correct before reporting hours."
            )
        if reported_crew and reported_crew != clocked_crew:
            conflicts.append(
                f"Reported {reported_crew} crew on site but {clocked_crew} clocked in. "
                "Confirm the headcount."
            )

    # --- scope and exclusions --------------------------------------------

    reported_text = " ".join(
        list(submission.get("work_areas", []) or [])
        + list(submission.get("activities", []) or [])
        + [str(submission.get("normalized_summary", ""))]
    )
    reported_words = _keywords(reported_text)
    for exclusion in project.get("exclusions", []) or []:
        overlap = _keywords(str(exclusion)) & reported_words
        if overlap:
            conflicts.append(
                f"Reported work overlaps a scope exclusion ({exclusion!r}) on "
                f"{', '.join(sorted(overlap))}. This may be extra work — escalate before "
                "reporting it as contract scope."
            )

    # --- duplicates -------------------------------------------------------

    for other in prior_submissions:
        if other.get("id") == submission.get("id"):
            continue
        same_person = other.get("submitted_by") and other.get("submitted_by") == submission.get("submitted_by")
        if same_person and str(other.get("work_date")) == work_date:
            conflicts.append(
                f"{submission.get('submitted_by_name') or 'This employee'} already submitted a "
                f"report for {work_date} ({other.get('id')}). Confirm whether this is an "
                "update or a duplicate."
            )
            break

    # --- contradiction with earlier reports -------------------------------

    in_progress_now = _keywords(" ".join(submission.get("work_areas", []) or []))
    for log in prior_logs:
        for bullet in log.get("completed", []) or []:
            if re.search(r"\bcomplete|finished\b", bullet, flags=re.IGNORECASE):
                overlap = _keywords(bullet) & in_progress_now
                if overlap and _in_progress_language(submission):
                    conflicts.append(
                        f"The daily log for {log.get('log_date')} reported this area complete "
                        f"({bullet!r}) but today's submission describes work still in progress "
                        f"there. Resolve before sending."
                    )
                    break

    # --- completeness ------------------------------------------------------

    if not submission.get("work_areas"):
        missing.append("No work area was identified — ask which area the work was performed in.")
    if not submission.get("activities"):
        missing.append("No work activity was identified — ask what was actually done.")
    if not reported_hours:
        missing.append("No labour hours were reported.")
    if not media:
        missing.append("No photos were submitted for this report.")
    language = str(submission.get("original_language", "unknown"))
    if language not in {"en", "unknown"} and not str(submission.get("translation_en", "")).strip():
        missing.append(f"The submission is in '{language}' and has not been translated yet.")
    if str(submission.get("kind")) == "voice" and not str(submission.get("transcript", "")).strip():
        missing.append("A voice recording was submitted but has not been transcribed.")

    if conflicts:
        state = Verification.CONFLICTED
    elif missing:
        state = Verification.UNVERIFIED
    else:
        state = Verification.VERIFIED

    return {
        "verification": state.value,
        "conflicts": conflicts,
        "missing": missing,
        "clocked_hours": clocked_hours,
        "clocked_crew": clocked_crew,
        "guidance": _guidance(state, conflicts, missing),
    }


def _in_progress_language(submission: dict[str, Any]) -> bool:
    text = " ".join(
        list(submission.get("activities", []) or []) + [str(submission.get("normalized_summary", ""))]
    ).lower()
    return bool(re.search(r"\bin progress|continuing|resumed|started|ongoing|remaining\b", text))


def _guidance(state: Verification, conflicts: list[str], missing: list[str]) -> str:
    if state is Verification.CONFLICTED:
        return (
            "Do not send anything external from this submission. Flag the conflict for review "
            "and ask the field employee a specific question — do not choose a version yourself."
        )
    if state is Verification.UNVERIFIED:
        return (
            "Ask the field employee for the missing detail. Send only the verified portion, and "
            "only where doing so is useful and safe."
        )
    return "Corroborated against the platform record. Safe to use as the basis for a report."
