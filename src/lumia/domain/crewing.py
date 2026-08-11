"""Matching people to work, deterministically.

Same argument as `scoring.py` and `pricing.py`: the model decides what
evidence exists, the code turns evidence into a number. The same crew facing
the same project must produce the same recommendation every time, and an
assignment a client or an employee questions has to be explainable from a
breakdown rather than from a paragraph the model wrote once.

Two things are deliberately *not* scored:

- Certifications. A missing or expired certification is a blocker, not a
  deduction. Points can be traded off; a fall-protection card cannot.
- Anything about a person that is not job-relevant. The factors below are
  the assignment rules from the operating spec and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .workforce import CrewRole, Employee, SkillLevel

#: 100 points across five job-relevant factors.
WEIGHTS = {
    "skill_match": 35,      # can they do this specific scope
    "productivity": 20,     # measured output against the planning baseline
    "reliability": 20,      # attendance and punctuality, from clock-in history
    "familiarity": 15,      # they already know this site and this client
    "leadership": 10,       # they can lead, on a project that needs leading
}

#: Days already worked on a project beyond which familiarity is fully credited.
FAMILIARITY_SATURATION_DAYS = 5

#: A project this size or this demanding needs a designated lead on site.
LEAD_REQUIRED_CREW_SIZE = 3
LEAD_REQUIRED_HOURS = 120.0

#: Someone at or below this level should not be alone on specialized scope.
SUPERVISION_THRESHOLD = SkillLevel.LEARNING


@dataclass
class FitScore:
    employee_id: str
    employee_name: str
    score: int
    breakdown: dict[str, int]
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "employee_name": self.employee_name,
            "score": self.score,
            "breakdown": self.breakdown,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "eligible": self.eligible,
        }


@dataclass
class CrewProposal:
    project_id: str
    crew: list[dict[str, Any]] = field(default_factory=list)
    alternates: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lead_employee_id: str = ""
    confidence: str = "medium"
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "crew": self.crew,
            "lead_employee_id": self.lead_employee_id,
            "alternates": self.alternates,
            "excluded": self.excluded,
            "warnings": self.warnings,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
        }


def requires_lead(project: dict[str, Any]) -> bool:
    """Complex or large projects need a qualified lead on site."""
    crew_size = int(project.get("crew_size_target", 2) or 2)
    hours = float(project.get("estimated_hours", 0) or 0)
    return (
        crew_size >= LEAD_REQUIRED_CREW_SIZE
        or hours >= LEAD_REQUIRED_HOURS
        or bool(project.get("includes_specialty_coating", False))
        or bool(project.get("is_occupied", False))
    )


def score_employee_fit(
    employee: Employee,
    project: dict[str, Any],
    *,
    prior_days_on_project: int = 0,
    on: date | None = None,
) -> FitScore:
    """Score one person against one project. Blockers make the score moot."""
    required_skills = [s for s in (project.get("required_skills") or []) if s]
    required_certs = [c for c in (project.get("required_certifications") or []) if c]
    work_date = on or date.today()

    # --- skill match ---------------------------------------------------
    if required_skills:
        levels = [employee.skill(skill) for skill in required_skills]
        ratio = sum(levels) / (len(levels) * int(SkillLevel.EXPERT))
    else:
        # No stated requirement: credit general trade capability instead of
        # awarding full marks to someone with no assessed skills at all.
        assessed = list(employee.skills.values()) or [0]
        ratio = sum(assessed) / (len(assessed) * int(SkillLevel.EXPERT))
    skill_points = round(WEIGHTS["skill_match"] * min(max(ratio, 0.0), 1.0))

    # --- productivity ---------------------------------------------------
    # 1.0 is on-standard and earns 70% of the band; 1.3+ earns full marks.
    factor = float(employee.productivity_factor or 1.0)
    productivity_ratio = min(max((factor - 0.6) / 0.7, 0.0), 1.0)
    productivity_points = round(WEIGHTS["productivity"] * productivity_ratio)

    # --- reliability ------------------------------------------------------
    reliability_points = round(WEIGHTS["reliability"] * min(max(float(employee.reliability or 0.0), 0.0), 1.0))

    # --- familiarity ------------------------------------------------------
    familiarity_ratio = min(prior_days_on_project / FAMILIARITY_SATURATION_DAYS, 1.0)
    familiarity_points = round(WEIGHTS["familiarity"] * familiarity_ratio)

    # --- leadership -------------------------------------------------------
    if requires_lead(project):
        leadership_points = WEIGHTS["leadership"] if employee.can_lead else 0
    else:
        # Not needed here, so it neither helps nor punishes.
        leadership_points = round(WEIGHTS["leadership"] * 0.5)

    breakdown = {
        "skill_match": skill_points,
        "productivity": productivity_points,
        "reliability": reliability_points,
        "familiarity": familiarity_points,
        "leadership": leadership_points,
    }

    blockers: list[str] = []
    warnings: list[str] = []

    if not employee.active:
        blockers.append("Employee is not active on the roster.")

    missing = employee.missing_certifications(required_certs, work_date)
    if missing:
        blockers.append(
            "Missing or expired certification: " + ", ".join(sorted(missing))
            + " — assigning anyway requires management approval."
        )

    for skill in required_skills:
        level = employee.skill(skill)
        if level == int(SkillLevel.NONE):
            warnings.append(f"No assessed capability in {skill}.")
        elif level <= int(SUPERVISION_THRESHOLD):
            warnings.append(f"Still learning {skill} — should not work it unsupervised.")

    for expiring in employee.expiring_certifications(work_date):
        if not expiring["expired"]:
            warnings.append(
                f"{expiring['certification']} expires in {expiring['days_remaining']} days "
                f"({expiring['expires_on']})."
            )

    return FitScore(
        employee_id=employee.id,
        employee_name=employee.name,
        score=sum(breakdown.values()),
        breakdown=breakdown,
        blockers=blockers,
        warnings=warnings,
    )


def assemble_crew(
    project: dict[str, Any],
    candidates: list[tuple[Employee, int]],
    *,
    crew_size: int = 0,
    on: date | None = None,
) -> CrewProposal:
    """Pick the strongest crew from the people who are actually available.

    `candidates` is (employee, prior_days_on_project) for people already
    confirmed available — availability is checked upstream, because a crew
    built from unavailable people is worse than no proposal at all.
    """
    project_id = str(project.get("id", ""))
    target = crew_size or int(project.get("crew_size_target", 2) or 2)
    needs_lead = requires_lead(project)

    scored = [
        score_employee_fit(employee, project, prior_days_on_project=days, on=on)
        for employee, days in candidates
    ]
    by_id = {employee.id: employee for employee, _ in candidates}

    eligible = sorted(
        [fit for fit in scored if fit.eligible],
        key=lambda fit: fit.score,
        reverse=True,
    )
    excluded = [fit.to_dict() for fit in scored if not fit.eligible]

    proposal = CrewProposal(project_id=project_id)
    if not eligible:
        proposal.warnings.append(
            "No eligible and available employee for this project — every candidate is "
            "blocked or unavailable. This needs a management decision, not a schedule."
        )
        proposal.excluded = excluded
        proposal.confidence = "low"
        proposal.reasoning = "No crew could be formed."
        return proposal

    chosen: list[Any] = []

    # A project that needs a lead gets one first, so leadership is not
    # crowded out by raw score.
    if needs_lead:
        lead = next((fit for fit in eligible if by_id[fit.employee_id].can_lead), None)
        if lead is not None:
            chosen.append(lead)
            proposal.lead_employee_id = lead.employee_id
        else:
            proposal.warnings.append(
                "This project needs a qualified team lead and none of the available "
                "employees can lead. Recommend reassigning a lead or deferring the start."
            )

    for fit in eligible:
        if len(chosen) >= target:
            break
        if fit not in chosen:
            chosen.append(fit)

    # Never leave a developing painter alone on the site.
    if len(chosen) == 1:
        only = by_id[chosen[0].employee_id]
        if only.crew_role is CrewRole.APPRENTICE or not only.works_alone:
            mentor = next(
                (fit for fit in eligible if fit not in chosen and by_id[fit.employee_id].can_lead),
                None,
            )
            if mentor is not None:
                chosen.append(mentor)
                proposal.warnings.append(
                    f"Paired {only.name} with {mentor.employee_name}: an apprentice should not "
                    "be alone on site."
                )
            else:
                proposal.warnings.append(
                    f"{only.name} would be alone on site without a qualified pair. "
                    "Recommend deferring rather than sending them solo."
                )

    proposal.crew = [fit.to_dict() for fit in chosen]
    proposal.alternates = [fit.to_dict() for fit in eligible if fit not in chosen][:5]
    proposal.excluded = excluded

    if not proposal.lead_employee_id and chosen:
        lead = next((fit for fit in chosen if by_id[fit.employee_id].can_lead), None)
        proposal.lead_employee_id = lead.employee_id if lead else ""

    if len(chosen) < target:
        proposal.warnings.append(
            f"Crew is short: {len(chosen)} available against a target of {target}. "
            "Expect the duration to stretch proportionally."
        )

    # Anyone still learning a required skill needs someone to learn from.
    developing = [
        fit for fit in chosen
        if any("Still learning" in w or "No assessed capability" in w for w in fit.warnings)
    ]
    if developing and not any(by_id[fit.employee_id].can_lead for fit in chosen):
        proposal.warnings.append(
            "Crew contains a developing painter with nobody able to supervise or train them."
        )

    proposal.confidence = _confidence(project, proposal, target, len(chosen))
    proposal.reasoning = _reasoning(project, proposal, chosen, needs_lead)
    return proposal


def _confidence(project: dict[str, Any], proposal: CrewProposal, target: int, actual: int) -> str:
    """Confidence in the crew recommendation, per the spec's three levels."""
    if not proposal.crew:
        return "low"
    if float(project.get("estimated_hours", 0) or 0) <= 0:
        return "low"
    if actual < target or proposal.warnings:
        return "medium"
    return "high"


def _reasoning(
    project: dict[str, Any],
    proposal: CrewProposal,
    chosen: list[FitScore],
    needs_lead: bool,
) -> str:
    if not chosen:
        return "No crew could be formed."
    names = ", ".join(f"{fit.employee_name} ({fit.score}/100)" for fit in chosen)
    parts = [f"Selected {names}."]
    if needs_lead and proposal.lead_employee_id:
        lead = next((f for f in chosen if f.employee_id == proposal.lead_employee_id), None)
        if lead is not None:
            parts.append(f"{lead.employee_name} leads — the project meets the lead-required threshold.")
    required = [s for s in (project.get("required_skills") or []) if s]
    if required:
        parts.append("Scored against required skills: " + ", ".join(required) + ".")
    if proposal.alternates:
        parts.append(
            "Next best available: "
            + ", ".join(a["employee_name"] for a in proposal.alternates[:3])
            + "."
        )
    return " ".join(parts)


__all__ = [
    "FAMILIARITY_SATURATION_DAYS",
    "LEAD_REQUIRED_CREW_SIZE",
    "LEAD_REQUIRED_HOURS",
    "WEIGHTS",
    "CrewProposal",
    "FitScore",
    "assemble_crew",
    "requires_lead",
    "score_employee_fit",
]
