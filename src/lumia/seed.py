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

    return {
        "seeded_accounts": created,
        "note": (
            "Demo records are tagged source='demo_seed' and named (DEMO). "
            "Delete the data/ directory before using this against real accounts."
        ),
        "pipeline": ws.crm.pipeline(),
    }
