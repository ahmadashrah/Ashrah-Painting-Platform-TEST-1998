"""The communication record of truth.

Everything the recordkeeping section requires lives here: the original
field submission and its language, the transcript, the translation, the
media, the final report, who it went to, on what channel, when, whether it
arrived, who approved it, what came back, and what was corrected
afterwards.

The one invariant this module protects is **append-only history**. A
submission's original text and language cannot be rewritten through this
API — corrections append a revision instead — and a sent communication is
never edited in place; a correction is a new record pointing back at the
one it replaces.
"""

from __future__ import annotations

from typing import Any

from ..domain.projects import (
    Communication,
    DeliveryStatus,
    Escalation,
    OpenItem,
    RecipientRole,
    now_iso,
    today_iso,
)
from ..store import LocalStore

#: Fields of a field submission that are written once and never rewritten.
IMMUTABLE_SUBMISSION_FIELDS = ("original_text", "original_language", "transcript", "submitted_at")


class CommunicationLedger:
    """Project communication state, backed by the same local store as the CRM."""

    def __init__(self, store: LocalStore) -> None:
        self.store = store

    # --- projects and people ---------------------------------------------

    def upsert_project(self, project: dict[str, Any]) -> dict[str, Any]:
        existing = self.store.get("projects", project["id"])
        if existing:
            # Blank means "not supplied" on a partial update.
            existing.update({k: v for k, v in project.items() if v not in (None, "", [], {})})
            project = existing
        return self.store.put("projects", project["id"], project)

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        return self.store.get("projects", project_id)

    def list_projects(self, **filters: Any) -> list[dict[str, Any]]:
        records = self.store.where("projects", **filters) if filters else self.store.list("projects")
        return sorted(records, key=lambda r: str(r.get("name", "")))

    def active_projects(self) -> list[dict[str, Any]]:
        return [p for p in self.list_projects() if p.get("live")]

    def find_projects(self, term: str) -> list[dict[str, Any]]:
        return self.store.search("projects", term)

    def upsert_contact(self, contact: dict[str, Any]) -> dict[str, Any]:
        return self.store.put("project_contacts", contact["id"], contact)

    def get_contact(self, contact_id: str) -> dict[str, Any] | None:
        return self.store.get("project_contacts", contact_id)

    def contacts_for(self, project_id: str) -> list[dict[str, Any]]:
        return self.store.where("project_contacts", project_id=project_id)

    def find_recipient(self, project_id: str, needle: str) -> dict[str, Any] | None:
        """Resolve a recipient by contact id, name or role — never by guesswork.

        The spec forbids inventing contact details, so a send tool that
        cannot resolve its recipient here refuses rather than proceeding
        with an address the model produced from memory.
        """
        needle = needle.strip().lower()
        if not needle:
            return None
        contacts = self.contacts_for(project_id)
        for contact in contacts:
            if contact["id"].lower() == needle:
                return contact
        for contact in contacts:
            if str(contact.get("name", "")).strip().lower() == needle:
                return contact
        for contact in contacts:
            if str(contact.get("role", "")).strip().lower() == needle:
                return contact
        for contact in contacts:
            if needle in str(contact.get("name", "")).strip().lower():
                return contact
        return None

    def daily_log_recipients(self, project_id: str) -> list[dict[str, Any]]:
        return [c for c in self.contacts_for(project_id) if c.get("receives_daily_log")]

    def upsert_crew(self, member: dict[str, Any]) -> dict[str, Any]:
        return self.store.put("crew", member["id"], member)

    def get_crew(self, crew_id: str) -> dict[str, Any] | None:
        return self.store.get("crew", crew_id)

    def crew_for(self, project_id: str) -> list[dict[str, Any]]:
        return [c for c in self.store.list("crew") if project_id in (c.get("project_ids") or [])]

    def find_crew(self, needle: str) -> dict[str, Any] | None:
        needle = needle.strip().lower()
        if not needle:
            return None
        for member in self.store.list("crew"):
            if member["id"].lower() == needle or str(member.get("name", "")).lower() == needle:
                return member
        return None

    # --- time records ------------------------------------------------------

    def record_time(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.put("time_records", record["id"], record)

    def time_records_for(self, project_id: str, work_date: str = "") -> list[dict[str, Any]]:
        records = self.store.where("time_records", project_id=project_id)
        if work_date:
            records = [r for r in records if str(r.get("work_date", "")) == work_date]
        return records

    # --- field submissions -------------------------------------------------

    def record_submission(self, submission: dict[str, Any]) -> dict[str, Any]:
        submission.setdefault("revisions", []).append(
            {"at": now_iso(), "action": "submitted", "by": submission.get("submitted_by_name", ""), "note": ""}
        )
        return self.store.put("submissions", submission["id"], submission)

    def get_submission(self, submission_id: str) -> dict[str, Any] | None:
        return self.store.get("submissions", submission_id)

    def submissions_for(self, project_id: str, work_date: str = "") -> list[dict[str, Any]]:
        records = self.store.where("submissions", project_id=project_id)
        if work_date:
            records = [r for r in records if str(r.get("work_date", "")) == work_date]
        return sorted(records, key=lambda r: str(r.get("submitted_at", "")))

    def attach_transcript(self, submission_id: str, text: str, *, source: str = "whisper") -> dict[str, Any]:
        """Write the transcript of a voice note, once.

        The transcript is an immutable field — it is what the recording
        says, and rewriting it would break the chain from recording to sent
        message. But a submission that arrived as audio has no transcript
        *yet*, and machine transcription is how it gets one. So this fills
        an empty transcript and refuses a populated one, rather than
        loosening the rule for everything.

        A correction to an existing transcript is a human decision: record
        it in `normalized_summary` with a note, where the revision trail
        shows what changed and why.
        """
        record = self.store.get("submissions", submission_id)
        if record is None:
            return {"error": f"no field submission with id {submission_id}"}
        if str(record.get("transcript", "")).strip():
            return {
                "error": (
                    "this submission already has a transcript, and the original is preserved. "
                    "Record a correction in the summary instead of overwriting what was heard."
                ),
                "existing_transcript": record["transcript"],
            }
        if not text.strip():
            return {"error": "transcription produced no text; the recording may be silent or unreadable"}

        revisions = list(record.get("revisions", []))
        revisions.append(
            {"at": now_iso(), "action": "transcribed", "by": source, "note": "", "fields": ["transcript"]}
        )
        updated = self.store.patch(
            "submissions", submission_id, {"transcript": text, "revisions": revisions}
        )
        return updated or {"error": f"no field submission with id {submission_id}"}

    def revise_submission(
        self,
        submission_id: str,
        changes: dict[str, Any],
        *,
        action: str,
        note: str = "",
        by: str = "lumia",
    ) -> dict[str, Any]:
        """Apply a change and append it to the revision trail.

        Refuses to touch the original text, language or transcript: what
        was submitted stays exactly as submitted.
        """
        record = self.store.get("submissions", submission_id)
        if record is None:
            return {"error": f"no field submission with id {submission_id}"}

        blocked = [
            f for f in IMMUTABLE_SUBMISSION_FIELDS
            if f in changes and str(changes[f]) != str(record.get(f, ""))
        ]
        if blocked:
            return {
                "error": (
                    f"cannot overwrite {', '.join(blocked)} — the original submission is "
                    "preserved for traceability. Record a correction instead."
                )
            }

        applied = {k: v for k, v in changes.items() if k not in IMMUTABLE_SUBMISSION_FIELDS}
        revisions = list(record.get("revisions", []))
        revisions.append(
            {
                "at": now_iso(),
                "action": action,
                "by": by,
                "note": note,
                "fields": sorted(applied),
            }
        )
        applied["revisions"] = revisions
        updated = self.store.patch("submissions", submission_id, applied)
        return updated or {"error": f"no field submission with id {submission_id}"}

    # --- media -------------------------------------------------------------

    def add_media(self, asset: dict[str, Any]) -> dict[str, Any]:
        return self.store.put("media", asset["id"], asset)

    def get_media_asset(self, media_id: str) -> dict[str, Any] | None:
        return self.store.get("media", media_id)

    def update_media(self, media_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        updated = self.store.patch("media", media_id, changes)
        return updated or {"error": f"no media asset with id {media_id}"}

    def media_for(self, project_id: str, submission_id: str = "") -> list[dict[str, Any]]:
        records = self.store.where("media", project_id=project_id)
        if submission_id:
            records = [m for m in records if m.get("submission_id") == submission_id]
        return sorted(records, key=lambda m: (int(m.get("sequence", 0)), str(m.get("id"))))

    # --- daily logs --------------------------------------------------------

    def save_daily_log(self, log: dict[str, Any]) -> dict[str, Any]:
        return self.store.put("daily_logs", log["id"], log)

    def get_daily_log(self, log_id: str) -> dict[str, Any] | None:
        return self.store.get("daily_logs", log_id)

    def daily_logs_for(self, project_id: str) -> list[dict[str, Any]]:
        return sorted(
            self.store.where("daily_logs", project_id=project_id),
            key=lambda r: str(r.get("log_date", "")),
        )

    def daily_log_on(self, project_id: str, log_date: str) -> dict[str, Any] | None:
        for log in self.daily_logs_for(project_id):
            if str(log.get("log_date")) == log_date:
                return log
        return None

    def previous_daily_log(self, project_id: str, before: str) -> dict[str, Any] | None:
        earlier = [log for log in self.daily_logs_for(project_id) if str(log.get("log_date", "")) < before]
        return earlier[-1] if earlier else None

    # --- communications ----------------------------------------------------

    def save_communication(self, communication: dict[str, Any]) -> dict[str, Any]:
        return self.store.put("communications", communication["id"], communication)

    def get_communication(self, communication_id: str) -> dict[str, Any] | None:
        return self.store.get("communications", communication_id)

    def communications_for(self, project_id: str = "", limit: int = 0) -> list[dict[str, Any]]:
        records = (
            self.store.where("communications", project_id=project_id)
            if project_id
            else self.store.list("communications")
        )
        ordered = sorted(records, key=lambda r: str(r.get("created_at", "")), reverse=True)
        return ordered[:limit] if limit else ordered

    def history_with(self, project_id: str, recipient: str) -> list[dict[str, Any]]:
        needle = recipient.strip().lower()
        return [
            c
            for c in self.communications_for(project_id)
            if needle in str(c.get("recipient_name", "")).lower()
            or needle == str(c.get("contact_id", "")).lower()
            or needle == str(c.get("recipient_role", "")).lower()
        ]

    def mark_sent(
        self,
        communication_id: str,
        delivery: dict[str, Any],
        *,
        approval_id: str = "",
        approval_source: str = "",
    ) -> dict[str, Any]:
        failed = bool(delivery.get("error")) or str(delivery.get("status", "")) == "failed"
        changes = {
            "status": (DeliveryStatus.FAILED if failed else DeliveryStatus.SENT).value,
            "sent_at": now_iso(),
            "delivery_detail": delivery,
            "approval_id": approval_id,
            "approval_source": approval_source,
        }
        updated = self.store.patch("communications", communication_id, changes)
        return updated or {"error": f"no communication with id {communication_id}"}

    def mark_awaiting_approval(self, communication_id: str, approval_id: str) -> dict[str, Any]:
        updated = self.store.patch(
            "communications",
            communication_id,
            {"status": DeliveryStatus.AWAITING_APPROVAL.value, "approval_id": approval_id},
        )
        return updated or {"error": f"no communication with id {communication_id}"}

    def record_response(
        self,
        communication_id: str,
        summary: str,
        *,
        received_on: str = "",
        sentiment: str = "neutral",
        raw: str = "",
    ) -> dict[str, Any]:
        record = self.store.get("communications", communication_id)
        if record is None:
            return {"error": f"no communication with id {communication_id}"}
        responses = list(record.get("responses", []))
        responses.append(
            {
                "summary": summary,
                "received_on": received_on or today_iso(),
                "sentiment": sentiment,
                "raw": raw,
                "logged_at": now_iso(),
            }
        )
        updated = self.store.patch(
            "communications",
            communication_id,
            {"responses": responses, "status": DeliveryStatus.REPLIED.value},
        )
        return updated or {"error": f"no communication with id {communication_id}"}

    def unanswered(self, project_id: str = "", as_of: str = "") -> list[dict[str, Any]]:
        """Sent messages that asked for something and have had no reply."""
        as_of = as_of or today_iso()
        pending = []
        for record in self.communications_for(project_id):
            if record.get("status") != DeliveryStatus.SENT.value:
                continue
            if not record.get("requires_response") or record.get("responses"):
                continue
            due = str(record.get("response_due", ""))
            pending.append({**record, "overdue": bool(due and due < as_of)})
        return pending

    def failed_deliveries(self, project_id: str = "") -> list[dict[str, Any]]:
        return [
            c for c in self.communications_for(project_id)
            if c.get("status") == DeliveryStatus.FAILED.value
        ]

    # --- open items --------------------------------------------------------

    def save_open_item(self, item: dict[str, Any]) -> dict[str, Any]:
        return self.store.put("open_items", item["id"], item)

    def get_open_item(self, item_id: str) -> dict[str, Any] | None:
        return self.store.get("open_items", item_id)

    def open_items(self, project_id: str = "", include_closed: bool = False) -> list[dict[str, Any]]:
        records = (
            self.store.where("open_items", project_id=project_id)
            if project_id
            else self.store.list("open_items")
        )
        if not include_closed:
            records = [r for r in records if r.get("status") == "open"]
        today = today_iso()
        return [{**r, "overdue": bool(r.get("due_date") and str(r["due_date"]) < today and r.get("status") == "open")} for r in records]

    def answer_open_item(self, item_id: str, answer: str, answered_on: str = "") -> dict[str, Any]:
        updated = self.store.patch(
            "open_items",
            item_id,
            {"status": "answered", "answer": answer, "answered_on": answered_on or today_iso()},
        )
        return updated or {"error": f"no open item with id {item_id}"}

    # --- escalations -------------------------------------------------------

    def save_escalation(self, escalation: dict[str, Any]) -> dict[str, Any]:
        return self.store.put("escalations", escalation["id"], escalation)

    def escalations(self, project_id: str = "", status: str = "") -> list[dict[str, Any]]:
        records = (
            self.store.where("escalations", project_id=project_id)
            if project_id
            else self.store.list("escalations")
        )
        if status:
            records = [r for r in records if r.get("status") == status]
        return sorted(records, key=lambda r: str(r.get("created_at", "")), reverse=True)


def blank_communication(project_id: str) -> Communication:
    """A Communication with the defaults a draft starts from."""
    return Communication(project_id=project_id, recipient_role=RecipientRole.CLIENT)


def blank_open_item(project_id: str, question: str) -> OpenItem:
    return OpenItem(project_id=project_id, question=question)


def blank_escalation(project_id: str, category: Any, summary: str) -> Escalation:
    return Escalation(project_id=project_id, category=category, summary=summary)
