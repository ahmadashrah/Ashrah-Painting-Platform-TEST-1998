"""B2B growth objects: accounts, contacts, interactions, opportunities, signals.

These are the nouns of the Lumia operating loop. Every marketing action
resolves to a change on one of them.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def today_iso() -> str:
    return date.today().isoformat()


class AccountTier(str, Enum):
    """Where research and personalization effort goes."""

    A = "A"  # Strategic — large lifetime value or high painting frequency
    B = "B"  # Growth — strong prospects, regular outreach
    C = "C"  # Prospecting — broader market, scalable campaigns
    UNCLASSIFIED = "unclassified"


class AccountType(str, Enum):
    GENERAL_CONTRACTOR = "general_contractor"
    PROPERTY_MANAGEMENT = "property_management"
    COMMERCIAL_OWNER = "commercial_owner"
    INSTITUTION = "institution"
    FACILITY_INTENSIVE = "facility_intensive"
    OTHER = "other"


class PipelineStage(str, Enum):
    PROSPECT = "prospect"
    RESEARCHED = "researched"
    CONTACTED = "contacted"
    ENGAGED = "engaged"
    MEETING = "meeting"
    ESTIMATING_OPPORTUNITY = "estimating_opportunity"
    ESTIMATE_SUBMITTED = "estimate_submitted"
    FOLLOW_UP = "follow_up"
    NEGOTIATION = "negotiation"
    WON = "won"
    LOST = "lost"
    NURTURE = "nurture"


#: Stages where the account is no longer being actively worked.
TERMINAL_STAGES = {PipelineStage.WON, PipelineStage.LOST, PipelineStage.NURTURE}


class Channel(str, Enum):
    EMAIL = "email"
    LINKEDIN = "linkedin"
    PHONE = "phone"
    MEETING = "meeting"
    EVENT = "event"
    REFERRAL = "referral"
    INBOUND = "inbound"
    TENDER_PORTAL = "tender_portal"


class Confidence(str, Enum):
    """Anti-hallucination discipline: every claim is labelled."""

    KNOWN_FACT = "known_fact"   # verified from a tool or record
    INFERENCE = "inference"     # reasonable conclusion from evidence
    UNKNOWN = "unknown"         # needs research; never asserted as true


@dataclass
class Account:
    name: str
    account_type: AccountType = AccountType.OTHER
    website: str = ""
    location: str = ""
    size_note: str = ""                     # "~40 properties", "120 staff"
    tier: AccountTier = AccountTier.UNCLASSIFIED
    stage: PipelineStage = PipelineStage.PROSPECT
    pain_points: list[str] = field(default_factory=list)
    likely_needs: list[str] = field(default_factory=list)
    estimated_annual_value: float = 0.0
    score: int = 0
    score_breakdown: dict[str, int] = field(default_factory=dict)
    next_action: str = ""
    next_action_date: str = ""
    last_interaction_date: str = ""
    notes: str = ""
    source: str = ""
    id: str = field(default_factory=lambda: new_id("acct"))
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def unmanaged(self) -> bool:
        """Spec rule: an active opportunity without a next action is unmanaged."""
        return self.stage not in TERMINAL_STAGES and not self.next_action.strip()

    def to_dict(self) -> dict[str, Any]:
        # `unmanaged` is deliberately not stored — it is derived from stage and
        # next_action, and a persisted copy goes stale the moment either changes.
        # Readers get it computed fresh (see CRM.get_account).
        return _serialize(self)


@dataclass
class Contact:
    account_id: str
    name: str
    title: str = ""
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    is_decision_maker: bool = False
    notes: str = ""
    id: str = field(default_factory=lambda: new_id("cont"))

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass
class Interaction:
    account_id: str
    channel: Channel
    direction: str            # "outbound" | "inbound"
    summary: str
    contact_id: str = ""
    outcome: str = ""         # "no_response", "positive_reply", "meeting_booked", ...
    occurred_on: str = field(default_factory=today_iso)
    id: str = field(default_factory=lambda: new_id("intx"))

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass
class Opportunity:
    account_id: str
    description: str
    estimated_value: float = 0.0
    stage: PipelineStage = PipelineStage.ESTIMATING_OPPORTUNITY
    probability: float = 0.2          # 0-1
    expected_decision_date: str = ""
    source_signal_id: str = ""
    id: str = field(default_factory=lambda: new_id("opp"))
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def weighted_value(self) -> float:
        return round(self.estimated_value * self.probability, 2)

    def to_dict(self) -> dict[str, Any]:
        # weighted_value is derived from estimated_value * probability; both can
        # change on update, so it is computed on read rather than stored.
        return _serialize(self)


@dataclass
class MarketSignal:
    """A raw market event, plus the action it implies.

    The spec is explicit: a signal that does not produce a recommended
    action is not finished work.
    """

    headline: str
    signal_type: str                  # permit, tender, lease, renovation, acquisition...
    source: str
    confidence: Confidence = Confidence.INFERENCE
    likely_account: str = ""
    who_controls_it: str = ""         # GC, property manager, owner — or "unknown"
    painting_likely: bool = False
    likely_timing: str = ""
    recommended_action: str = ""
    observed_on: str = field(default_factory=today_iso)
    id: str = field(default_factory=lambda: new_id("sig"))

    @property
    def actionable(self) -> bool:
        return bool(self.recommended_action.strip())

    def to_dict(self) -> dict[str, Any]:
        data = _serialize(self)
        data["actionable"] = self.actionable
        return data


@dataclass
class ARERecord:
    """One pass of the Action → Reasoning → Evaluation loop."""

    action: str
    objective: str
    target: str
    expected_result: str
    measurement: str
    reasoning: str = ""
    result: str = ""
    evaluation: str = ""
    lesson: str = ""
    confidence: Confidence = Confidence.INFERENCE
    level: str = "action"             # action | campaign | strategic
    account_id: str = ""
    recorded_on: str = field(default_factory=today_iso)
    id: str = field(default_factory=lambda: new_id("are"))

    @property
    def closed(self) -> bool:
        return bool(self.result.strip() and self.evaluation.strip())

    def to_dict(self) -> dict[str, Any]:
        data = _serialize(self)
        data["closed"] = self.closed
        return data


def _serialize(obj: Any) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v) for v in value]
        return value

    return {k: convert(v) for k, v in asdict(obj).items()}
