"""Marketing Learning Memory and the ARE ledger.

Two jobs:

1. Keep a ledger of ARE passes so an action's expected result can be
   compared against what actually happened.
2. Accumulate lessons, and let the agent retrieve relevant ones *before*
   it repeats an experiment. Without retrieval the memory is a diary; with
   it, the system compounds.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .domain.accounts import ARERecord, Confidence, new_id, today_iso
from .store import LocalStore


@dataclass
class Lesson:
    """A durable, reusable finding — not a one-off observation."""

    observation: str            # what objectively happened
    hypothesis: str             # what may explain it
    evidence: str               # what supports the hypothesis
    confidence: Confidence = Confidence.INFERENCE
    segment: str = ""           # "general_contractor", "property_management", ...
    channel: str = ""           # email, linkedin, phone...
    sample_size: int = 1
    revenue_outcome: float = 0.0
    recommended_use: str = ""
    scope: str = "general"      # "general" learning vs account-specific "preference"
    account_id: str = ""
    recorded_on: str = field(default_factory=today_iso)
    id: str = field(default_factory=lambda: new_id("lesn"))

    @property
    def strength(self) -> str:
        """One interaction is not a rule. Strength reflects repeated evidence."""
        if self.sample_size >= 30:
            return "high"
        if self.sample_size >= 8:
            return "medium"
        return "low"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["confidence"] = self.confidence.value if isinstance(self.confidence, Confidence) else str(self.confidence)
        data["strength"] = self.strength
        return data


@dataclass
class ImprovementProposal:
    """A Lumia Improvement Proposal, per the governance section of the spec."""

    problem: str
    evidence: str
    root_cause_hypothesis: str
    proposed_change: str
    affected_component: str      # prompt | workflow | agent | tool | memory | crm | evaluation
    expected_improvement: str
    risk: str
    test_plan: str
    rollback: str
    status: str = "proposed"     # proposed | testing | adopted | rejected
    id: str = field(default_factory=lambda: new_id("lip"))
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Memory:
    def __init__(self, store: LocalStore) -> None:
        self.store = store

    # --- ARE ledger ----------------------------------------------------

    def open_are(self, record: ARERecord) -> dict[str, Any]:
        data = record.to_dict()
        self.store.put("are_records", record.id, data)
        return data

    def close_are(self, are_id: str, result: str, evaluation: str, lesson: str, confidence: str) -> dict[str, Any]:
        updated = self.store.patch(
            "are_records",
            are_id,
            {
                "result": result,
                "evaluation": evaluation,
                "lesson": lesson,
                "confidence": confidence,
                "closed": bool(result.strip() and evaluation.strip()),
            },
        )
        if updated is None:
            return {"error": f"no ARE record with id {are_id}"}
        return updated

    def open_are_records(self) -> list[dict[str, Any]]:
        return [r for r in self.store.list("are_records") if not r.get("closed")]

    # --- lessons -------------------------------------------------------

    def record_lesson(self, lesson: Lesson) -> dict[str, Any]:
        """Store a lesson, merging into an existing one where they match.

        Merging is what turns repeated observations into evidence: the same
        finding seen again raises the sample size and the strength rather
        than creating a second, competing record.
        """
        for existing in self.store.list("lessons"):
            if (
                existing.get("scope") == lesson.scope
                and existing.get("segment") == lesson.segment
                and existing.get("channel") == lesson.channel
                and _similar(str(existing.get("hypothesis", "")), lesson.hypothesis)
            ):
                merged = dict(existing)
                merged["sample_size"] = int(existing.get("sample_size", 1)) + lesson.sample_size
                merged["evidence"] = f"{existing.get('evidence', '')}; {lesson.evidence}".strip("; ")
                merged["revenue_outcome"] = round(
                    float(existing.get("revenue_outcome", 0)) + lesson.revenue_outcome, 2
                )
                merged["strength"] = Lesson(
                    observation=lesson.observation,
                    hypothesis=lesson.hypothesis,
                    evidence=lesson.evidence,
                    sample_size=merged["sample_size"],
                ).strength
                self.store.put("lessons", merged["id"], merged)
                return merged

        data = lesson.to_dict()
        self.store.put("lessons", lesson.id, data)
        return data

    def recall(
        self,
        query: str = "",
        *,
        segment: str = "",
        channel: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Retrieve the lessons most relevant to a decision about to be made."""
        candidates = self.store.list("lessons")

        def relevance(lesson: dict[str, Any]) -> tuple[int, int, int]:
            score = 0
            if segment and lesson.get("segment") == segment:
                score += 3
            if channel and lesson.get("channel") == channel:
                score += 2
            if query:
                text = " ".join(
                    str(lesson.get(k, "")) for k in ("observation", "hypothesis", "recommended_use")
                ).lower()
                score += sum(1 for word in query.lower().split() if len(word) > 3 and word in text)
            strength_rank = {"high": 3, "medium": 2, "low": 1}.get(str(lesson.get("strength", "low")), 1)
            return (score, strength_rank, int(lesson.get("sample_size", 1)))

        ranked = sorted(candidates, key=relevance, reverse=True)
        # Drop anything with no relevance at all when a query was supplied.
        if query or segment or channel:
            ranked = [lesson for lesson in ranked if relevance(lesson)[0] > 0] or ranked[:limit]
        return ranked[:limit]

    # --- improvement proposals -----------------------------------------

    def propose(self, proposal: ImprovementProposal) -> dict[str, Any]:
        data = proposal.to_dict()
        self.store.put("proposals", proposal.id, data)
        return data

    def proposals(self, status: str = "") -> list[dict[str, Any]]:
        records = self.store.list("proposals")
        return [p for p in records if p.get("status") == status] if status else records


def _similar(a: str, b: str) -> bool:
    """Cheap overlap check — enough to merge restatements of one finding."""
    a_words = {w for w in a.lower().split() if len(w) > 3}
    b_words = {w for w in b.lower().split() if len(w) > 3}
    if not a_words or not b_words:
        return False
    overlap = len(a_words & b_words) / len(a_words | b_words)
    return overlap >= 0.6
