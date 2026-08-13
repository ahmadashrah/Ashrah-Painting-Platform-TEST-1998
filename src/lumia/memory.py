"""Marketing Learning Memory and the ARE ledger.

Two jobs:

1. Keep a ledger of ARE passes so an action's expected result can be
   compared against what actually happened.
2. Accumulate lessons, and let the agent retrieve relevant ones *before*
   it repeats an experiment. Without retrieval the memory is a diary; with
   it, the system compounds.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .domain.accounts import ARERecord, Confidence, new_id, today_iso
from .integrations.openai import cosine
from .store import LocalStore

log = logging.getLogger(__name__)

#: Below this similarity a lesson is not really about the question asked.
#: Set deliberately low: a near-miss lesson shown and judged irrelevant costs
#: a glance, while a relevant one withheld costs a repeated mistake.
SIMILARITY_FLOOR = 0.25


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
    """The ARE ledger and the lessons, with recall on top.

    Recall has two modes. With an embedder configured, a lesson is matched
    by meaning — "the GC stopped replying after long emails" surfaces for
    "how should I write to Northgate" without sharing a word. Without one,
    it falls back to keyword overlap, which is weaker but needs no
    credentials and never silently disappears.

    The fallback matters more than the upgrade: a memory that returns
    nothing when an API key is missing would quietly stop influencing
    decisions, and nobody would notice.
    """

    def __init__(self, store: LocalStore, embedder: Any = None, model: str = "") -> None:
        self.store = store
        self.embedder = embedder
        self.model = model

    @property
    def semantic(self) -> bool:
        return bool(self.embedder is not None and getattr(self.embedder, "live", False))

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not self.semantic or not texts:
            return []
        try:
            result = self.embedder.embed(texts, model=self.model) if self.model else self.embedder.embed(texts)
        except Exception as exc:  # embedding is an optimization, never a hard dependency
            log.warning("embedding failed, falling back to keyword recall: %s", exc)
            return []
        return list(result.get("vectors") or [])

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
        vectors = self._embed([_lesson_text(data)])
        if vectors:
            data["embedding"] = vectors[0]
        self.store.put("lessons", lesson.id, data)
        # The stored vector is an implementation detail; callers get the lesson.
        return {k: v for k, v in data.items() if k != "embedding"}

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

        if query and self.semantic:
            ranked = self._recall_by_meaning(query, candidates, segment=segment, channel=channel)
            if ranked is not None:
                return ranked[:limit]

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
        return [_without_vector(lesson) for lesson in ranked[:limit]]

    def _recall_by_meaning(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        *,
        segment: str = "",
        channel: str = "",
    ) -> list[dict[str, Any]] | None:
        """Rank by similarity. Returns None to fall back on keyword matching.

        Lessons stored before an embedder was configured have no vector, so
        they are embedded on demand and written back rather than being
        excluded — otherwise turning embeddings on would make the oldest and
        best-evidenced lessons invisible.
        """
        query_vectors = self._embed([query])
        if not query_vectors:
            return None
        query_vector = query_vectors[0]

        missing = [c for c in candidates if not c.get("embedding")]
        if missing:
            filled = self._embed([_lesson_text(c) for c in missing])
            for lesson, vector in zip(missing, filled):
                lesson["embedding"] = vector
                self.store.put("lessons", lesson["id"], lesson)

        scored = []
        for lesson in candidates:
            vector = lesson.get("embedding")
            if not vector:
                continue
            score = cosine(query_vector, vector)
            # Filters are still filters: a matching segment or channel lifts a
            # lesson, it does not let an unrelated one through.
            if segment and lesson.get("segment") == segment:
                score += 0.15
            if channel and lesson.get("channel") == channel:
                score += 0.1
            if score >= SIMILARITY_FLOOR:
                scored.append((score, lesson))

        if not scored:
            return None
        strength = {"high": 3, "medium": 2, "low": 1}
        scored.sort(
            key=lambda pair: (round(pair[0], 3), strength.get(str(pair[1].get("strength", "low")), 1)),
            reverse=True,
        )
        return [
            {**_without_vector(lesson), "similarity": round(score, 3), "matched_by": "meaning"}
            for score, lesson in scored
        ]

    # --- improvement proposals -----------------------------------------

    def propose(self, proposal: ImprovementProposal) -> dict[str, Any]:
        data = proposal.to_dict()
        self.store.put("proposals", proposal.id, data)
        return data

    def proposals(self, status: str = "") -> list[dict[str, Any]]:
        records = self.store.list("proposals")
        return [p for p in records if p.get("status") == status] if status else records


def _lesson_text(lesson: dict[str, Any]) -> str:
    """What a lesson is *about*, for embedding purposes."""
    return " ".join(
        str(lesson.get(key, ""))
        for key in ("observation", "hypothesis", "recommended_use", "segment", "channel")
    ).strip()


def _without_vector(lesson: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in lesson.items() if k != "embedding"}


def _similar(a: str, b: str) -> bool:
    """Cheap overlap check — enough to merge restatements of one finding."""
    a_words = {w for w in a.lower().split() if len(w) > 3}
    b_words = {w for w in b.lower().split() if len(w) > 3}
    if not a_words or not b_words:
        return False
    overlap = len(a_words & b_words) / len(a_words | b_words)
    return overlap >= 0.6
