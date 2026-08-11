"""Lead scoring and account tiering.

Scoring is deterministic Python rather than a model judgement call: the
same account with the same evidence must always produce the same score, so
priority decisions are auditable and comparable week over week. The agent
decides *what evidence exists*; this module turns evidence into a number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .accounts import Account, AccountTier, AccountType, Interaction, PipelineStage

#: The six factors from the operating spec, and their weight out of 100.
WEIGHTS = {
    "fit": 20,           # how closely they match Ashrah's target customer
    "opportunity": 20,   # evidence of upcoming painting requirements
    "value": 20,         # potential annual / lifetime revenue
    "timing": 15,        # how soon work could occur
    "relationship": 15,  # do we already know them
    "engagement": 10,    # have they responded, asked, or invited us to bid
}

#: Account types ranked by how repeatedly they buy painting.
FIT_BY_TYPE = {
    AccountType.GENERAL_CONTRACTOR: 1.0,
    AccountType.PROPERTY_MANAGEMENT: 1.0,
    AccountType.COMMERCIAL_OWNER: 0.85,
    AccountType.INSTITUTION: 0.8,
    AccountType.FACILITY_INTENSIVE: 0.75,
    AccountType.OTHER: 0.25,
}

#: Annual value bands, in dollars, mapped to a 0-1 score.
VALUE_BANDS = [
    (250_000, 1.0),
    (100_000, 0.85),
    (50_000, 0.7),
    (25_000, 0.5),
    (10_000, 0.3),
    (0, 0.15),
]

#: Timing windows in days from now.
TIMING_BANDS = [
    (30, 1.0),
    (90, 0.8),
    (180, 0.55),
    (365, 0.3),
]

#: Interaction outcomes that count as genuine engagement.
POSITIVE_OUTCOMES = {
    "positive_reply",
    "meeting_booked",
    "estimate_requested",
    "tender_invitation",
    "vendor_registration",
    "referral",
    "won",
}

#: Pipeline stages carry their own engagement signal.
STAGE_ENGAGEMENT = {
    PipelineStage.PROSPECT: 0.0,
    PipelineStage.RESEARCHED: 0.0,
    PipelineStage.CONTACTED: 0.15,
    PipelineStage.ENGAGED: 0.5,
    PipelineStage.MEETING: 0.7,
    PipelineStage.ESTIMATING_OPPORTUNITY: 0.85,
    PipelineStage.ESTIMATE_SUBMITTED: 0.9,
    PipelineStage.FOLLOW_UP: 0.8,
    PipelineStage.NEGOTIATION: 1.0,
    PipelineStage.WON: 1.0,
    PipelineStage.LOST: 0.3,
    PipelineStage.NURTURE: 0.2,
}


@dataclass
class ScoreResult:
    total: int
    breakdown: dict[str, int]
    factors: dict[str, float]
    rationale: list[str]
    suggested_tier: AccountTier

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.total,
            "breakdown": self.breakdown,
            "factors": {k: round(v, 2) for k, v in self.factors.items()},
            "rationale": self.rationale,
            "suggested_tier": self.suggested_tier.value,
        }


def score_account(
    account: Account,
    *,
    interactions: list[Interaction] | None = None,
    open_signals: int = 0,
    days_until_opportunity: int | None = None,
    worked_together_before: bool = False,
) -> ScoreResult:
    """Score one account 0-100 across the six spec factors."""
    interactions = interactions or []
    rationale: list[str] = []

    # --- Fit -----------------------------------------------------------
    fit = FIT_BY_TYPE.get(account.account_type, 0.25)
    if account.account_type is AccountType.OTHER:
        rationale.append("Account type is unclassified — fit is capped until it is researched.")

    # --- Opportunity ---------------------------------------------------
    evidence = open_signals + len(account.likely_needs)
    opportunity = min(1.0, evidence * 0.25)
    if evidence == 0:
        rationale.append("No recorded project signal or known need — opportunity is unproven.")

    # --- Value ---------------------------------------------------------
    value = 0.15
    for threshold, points in VALUE_BANDS:
        if account.estimated_annual_value >= threshold:
            value = points
            break
    if account.estimated_annual_value <= 0:
        rationale.append("Estimated annual value not set; scored at the floor.")

    # --- Timing --------------------------------------------------------
    if days_until_opportunity is None:
        timing = 0.2
        rationale.append("Timing unknown — treated as distant until a date is found.")
    else:
        timing = 0.15
        for window, points in TIMING_BANDS:
            if days_until_opportunity <= window:
                timing = points
                break

    # --- Relationship --------------------------------------------------
    relationship = 0.0
    if worked_together_before:
        relationship = 1.0
        rationale.append("Existing customer — warm relationship outranks cold prospecting.")
    elif interactions:
        relationship = min(0.8, 0.25 + 0.15 * len(interactions))
        rationale.append(f"{len(interactions)} prior interaction(s) on record — do not cold-open.")

    # --- Engagement ----------------------------------------------------
    positive = sum(1 for i in interactions if i.outcome in POSITIVE_OUTCOMES)
    engagement = max(
        STAGE_ENGAGEMENT.get(account.stage, 0.0),
        min(1.0, positive * 0.4),
    )

    factors = {
        "fit": fit,
        "opportunity": opportunity,
        "value": value,
        "timing": timing,
        "relationship": relationship,
        "engagement": engagement,
    }
    breakdown = {name: round(factors[name] * weight) for name, weight in WEIGHTS.items()}
    total = min(100, sum(breakdown.values()))

    return ScoreResult(
        total=total,
        breakdown=breakdown,
        factors=factors,
        rationale=rationale,
        suggested_tier=suggest_tier(total, account.estimated_annual_value),
    )


def suggest_tier(score: int, estimated_annual_value: float) -> AccountTier:
    """Tier drives how much research and personalization an account earns."""
    if score >= 70 or estimated_annual_value >= 150_000:
        return AccountTier.A
    if score >= 45 or estimated_annual_value >= 40_000:
        return AccountTier.B
    return AccountTier.C


def prioritize(scored: list[tuple[Account, ScoreResult]]) -> list[tuple[Account, ScoreResult]]:
    """Rank accounts for the day's work.

    Sorts by tier first, then score — the spec's rule is that high-value
    accounts must not sit unattended while low-value ones get worked.
    """
    tier_rank = {AccountTier.A: 0, AccountTier.B: 1, AccountTier.C: 2, AccountTier.UNCLASSIFIED: 3}
    return sorted(
        scored,
        key=lambda pair: (
            tier_rank.get(pair[0].tier, 3),
            -pair[1].total,
            pair[0].name.lower(),
        ),
    )
