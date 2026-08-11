"""Scoring and pricing must be reproducible — that is why they aren't prompts."""

from __future__ import annotations

import pytest

from lumia.domain.accounts import (
    Account,
    AccountTier,
    AccountType,
    Channel,
    Interaction,
    MarketSignal,
    PipelineStage,
)
from lumia.domain.models import JobType, Surface, SurfaceCondition
from lumia.domain.pricing import (
    MINIMUM_JOB_TOTAL,
    build_quote,
    estimate_duration_days,
    estimate_surface,
)
from lumia.domain.scoring import prioritize, score_account, suggest_tier


# --- scoring -------------------------------------------------------------


def test_scoring_is_deterministic():
    account = Account(name="Acme GC", account_type=AccountType.GENERAL_CONTRACTOR, estimated_annual_value=120_000)
    first = score_account(account)
    second = score_account(account)
    assert first.total == second.total
    assert first.breakdown == second.breakdown


def test_score_stays_within_bounds():
    account = Account(
        name="Everything",
        account_type=AccountType.GENERAL_CONTRACTOR,
        estimated_annual_value=5_000_000,
        stage=PipelineStage.NEGOTIATION,
        likely_needs=["a", "b", "c", "d", "e"],
    )
    interactions = [
        Interaction(account_id=account.id, channel=Channel.EMAIL, direction="inbound", summary="x", outcome="won")
        for _ in range(10)
    ]
    result = score_account(
        account, interactions=interactions, open_signals=10, days_until_opportunity=1, worked_together_before=True
    )
    assert 0 <= result.total <= 100


def test_relationship_beats_cold_prospect():
    warm = Account(name="Warm", account_type=AccountType.PROPERTY_MANAGEMENT, estimated_annual_value=60_000)
    cold = Account(name="Cold", account_type=AccountType.PROPERTY_MANAGEMENT, estimated_annual_value=60_000)

    warm_result = score_account(warm, worked_together_before=True)
    cold_result = score_account(cold)

    assert warm_result.total > cold_result.total
    assert any("warm relationship" in note.lower() for note in warm_result.rationale)


def test_unknown_timing_is_flagged_not_assumed():
    result = score_account(Account(name="X", account_type=AccountType.INSTITUTION))
    assert any("Timing unknown" in note for note in result.rationale)


def test_tier_thresholds():
    assert suggest_tier(80, 0) is AccountTier.A
    assert suggest_tier(0, 200_000) is AccountTier.A
    assert suggest_tier(50, 0) is AccountTier.B
    assert suggest_tier(10, 0) is AccountTier.C


def test_prioritize_puts_tier_a_first():
    a = Account(name="Tier A", tier=AccountTier.A)
    c = Account(name="Tier C", tier=AccountTier.C, estimated_annual_value=999_999)
    scored = [(c, score_account(c)), (a, score_account(a))]
    assert prioritize(scored)[0][0].name == "Tier A"


# --- unmanaged accounts ---------------------------------------------------


def test_active_account_without_next_action_is_unmanaged():
    assert Account(name="X", stage=PipelineStage.ENGAGED).unmanaged is True
    assert Account(name="X", stage=PipelineStage.ENGAGED, next_action="Call Tuesday").unmanaged is False
    # Closed accounts don't need one.
    assert Account(name="X", stage=PipelineStage.WON).unmanaged is False


def test_signal_without_action_is_not_actionable():
    signal = MarketSignal(headline="New build announced", signal_type="permit", source="test")
    assert signal.actionable is False
    signal.recommended_action = "Email the GC's estimator this week."
    assert signal.actionable is True


# --- pricing --------------------------------------------------------------


def test_poor_condition_costs_more_labor_than_good():
    good = estimate_surface(Surface(name="w", square_feet=1000, condition=SurfaceCondition.GOOD), JobType.INTERIOR)
    poor = estimate_surface(Surface(name="w", square_feet=1000, condition=SurfaceCondition.POOR), JobType.INTERIOR)
    assert poor.labor_hours > good.labor_hours


def test_height_surcharge_applies_over_ten_feet():
    standard = estimate_surface(Surface(name="w", square_feet=1000, height_feet=9), JobType.INTERIOR)
    tall = estimate_surface(Surface(name="w", square_feet=1000, height_feet=20), JobType.INTERIOR)
    assert tall.labor_hours > standard.labor_hours


def test_primer_increases_paint_and_labor():
    plain = estimate_surface(Surface(name="w", square_feet=1000), JobType.INTERIOR)
    primed = estimate_surface(Surface(name="w", square_feet=1000, needs_primer=True), JobType.INTERIOR)
    assert primed.gallons > plain.gallons
    assert primed.labor_hours > plain.labor_hours


def test_quote_totals_are_internally_consistent():
    quote = build_quote(
        lead_id="acct_1",
        job_type=JobType.INTERIOR,
        surfaces=[Surface(name="Walls", square_feet=4000)],
    )
    assert quote.total > quote.subtotal
    # total = direct cost + overhead + margin
    assert quote.total == pytest.approx(quote.subtotal + quote.overhead + quote.margin, abs=0.05)


def test_tiny_job_hits_the_minimum():
    quote = build_quote(
        lead_id="acct_1",
        job_type=JobType.INTERIOR,
        surfaces=[Surface(name="Closet", square_feet=20)],
    )
    assert quote.total == pytest.approx(MINIMUM_JOB_TOTAL)


def test_quote_requires_a_surface():
    with pytest.raises(ValueError):
        build_quote(lead_id="acct_1", job_type=JobType.INTERIOR, surfaces=[])


def test_discount_reduces_total():
    surfaces = [Surface(name="Walls", square_feet=4000)]
    full = build_quote(lead_id="a", job_type=JobType.INTERIOR, surfaces=surfaces)
    discounted = build_quote(lead_id="a", job_type=JobType.INTERIOR, surfaces=surfaces, discount_rate=0.10)
    assert discounted.total < full.total


def test_duration_rounds_up_to_whole_days():
    assert estimate_duration_days(1, crew_size=2) == 1
    assert estimate_duration_days(16, crew_size=2) == 1
    assert estimate_duration_days(17, crew_size=2) == 2
    assert estimate_duration_days(0) == 0


def test_duration_rejects_empty_crew():
    with pytest.raises(ValueError):
        estimate_duration_days(10, crew_size=0)
