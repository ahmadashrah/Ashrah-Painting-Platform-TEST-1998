"""The tool surface the Communication Agent acts through.

Mixed into `Toolbox`, so these register alongside the growth tools and pass
through the same autonomy gate.

Three structural rules are worth reading before the code:

1. **Preparing and sending are different tools.** `draft_communication`
   only writes a record. `send_communication` takes a draft id and nothing
   else that matters — the body it screens and transmits is the *stored*
   one, so a draft cannot be screened clean and then sent as something
   different.
2. **Recipients are resolved, never composed.** Every send resolves its
   recipient against the project's stored contacts. There is no parameter
   for a free-text email address, so an address the model half-remembers
   cannot become an outbound message.
3. **The agent cannot write its own corroboration.** Clock records and
   project scope are platform data. No tool here creates a time record, so
   the verification step in `verification.py` cannot be satisfied by the
   agent agreeing with itself.
"""

from __future__ import annotations

from typing import Any

from ..autonomy import AUTO_SENDABLE_KINDS
from ..domain.projects import (
    BLOCKING_MEDIA_FLAGS,
    CommChannel,
    Communication,
    CommKind,
    CrewMember,
    DailyLog,
    DeliveryStatus,
    Escalation,
    EscalationCategory,
    FieldSubmission,
    MediaAsset,
    MediaFlag,
    MediaKind,
    OpenItem,
    OpenItemKind,
    Project,
    ProjectContact,
    ProjectStatus,
    RecipientRole,
    Severity,
    SubmissionKind,
    Verification,
    is_external,
    new_id,
    today_iso,
)
from ..memory import Lesson
from ..schema import array, boolean, integer, number, obj, string
from . import dailylog as daily
from .channels import choose_channel
from .reporting import communication_review
from .screening import screen, screen_record
from .verification import verify

def _enum(enum_cls: Any, value: Any, fallback: Any) -> Any:
    try:
        return enum_cls(value)
    except ValueError:
        return fallback


class CommunicationTools:
    """Project-communication tools. Mixed into `Toolbox`."""

    # Provided by Toolbox.
    ws: Any
    register: Any

    # --- projects and people (Level 1 reads) ------------------------------

    def _list_projects(self, status: str = "") -> dict[str, Any]:
        projects = (
            self.ws.comms.list_projects(status=status) if status else self.ws.comms.list_projects()
        )
        today = today_iso()
        rows = []
        for project in projects:
            log = self.ws.comms.daily_log_on(project["id"], today)
            rows.append(
                {
                    "id": project["id"],
                    "name": project.get("name", ""),
                    "number": project.get("number", ""),
                    "status": project.get("status", ""),
                    "address": project.get("address", ""),
                    "daily_log_today": (log or {}).get("status", "not started"),
                    "submissions_today": len(self.ws.comms.submissions_for(project["id"], today)),
                    "open_items": len(self.ws.comms.open_items(project["id"])),
                    "open_escalations": len(self.ws.comms.escalations(project["id"], status="open")),
                }
            )
        return {"count": len(rows), "projects": rows}

    def _get_project(self, project_id: str) -> dict[str, Any]:
        project = self.ws.comms.get_project(project_id)
        if project is None:
            return {"error": f"no project with id {project_id}"}
        return {
            "project": project,
            "contacts": self.ws.comms.contacts_for(project_id),
            "crew": self.ws.comms.crew_for(project_id),
            "open_items": self.ws.comms.open_items(project_id),
            "open_escalations": self.ws.comms.escalations(project_id, status="open"),
            "recent_communications": [
                {k: c.get(k) for k in ("id", "kind", "channel", "recipient_name", "subject", "status", "sent_at")}
                for c in self.ws.comms.communications_for(project_id, limit=10)
            ],
            "preferences": self.ws.memory.recall(query=project.get("name", ""), limit=5),
            "reminder": (
                "Communicate only from the records above. A recipient not listed here cannot "
                "be messaged — ask for their details rather than guessing an address."
            ),
        }

    def _upsert_project(
        self,
        name: str,
        project_id: str = "",
        number: str = "",
        address: str = "",
        client_name: str = "",
        client_account_id: str = "",
        general_contractor: str = "",
        scope_summary: str = "",
        exclusions: list[str] | None = None,
        status: str = "active",
        site_access_notes: str = "",
        preferred_channel: str = "email",
        daily_log_time: str = "",
        report_length: str = "standard",
        share_crew_count: bool = True,
        share_work_period: bool = True,
        notes: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        existing = self.ws.comms.get_project(project_id) if project_id else None
        project = Project(
            name=name,
            number=number,
            address=address,
            client_name=client_name,
            client_account_id=client_account_id,
            general_contractor=general_contractor,
            scope_summary=scope_summary,
            exclusions=exclusions or [],
            status=_enum(ProjectStatus, status, ProjectStatus.ACTIVE),
            site_access_notes=site_access_notes,
            preferred_channel=_enum(CommChannel, preferred_channel, CommChannel.EMAIL),
            daily_log_time=daily_log_time,
            report_length=report_length,
            share_crew_count=share_crew_count,
            share_work_period=share_work_period,
            notes=notes,
            source=source,
            id=(existing or {}).get("id") or project_id or new_id("proj"),
        )
        saved = self.ws.comms.upsert_project(project.to_dict())
        saved["_was_update"] = existing is not None
        return saved

    def _upsert_project_contact(
        self,
        project_id: str,
        name: str,
        role: str = "client",
        company: str = "",
        email: str = "",
        phone: str = "",
        preferred_channel: str = "email",
        receives_daily_log: bool = False,
        notes: str = "",
        contact_id: str = "",
    ) -> dict[str, Any]:
        if self.ws.comms.get_project(project_id) is None:
            return {"error": f"no project with id {project_id}; create the project first"}
        if not email.strip() and not phone.strip():
            return {
                "error": "a contact needs a verified email or phone number — record only "
                "details you actually have, never a guessed address"
            }
        contact = ProjectContact(
            project_id=project_id,
            name=name,
            role=_enum(RecipientRole, role, RecipientRole.CLIENT),
            company=company,
            email=email,
            phone=phone,
            preferred_channel=_enum(CommChannel, preferred_channel, CommChannel.EMAIL),
            receives_daily_log=receives_daily_log,
            notes=notes,
            id=contact_id or new_id("pcon"),
        )
        return self.ws.comms.upsert_contact(contact.to_dict())

    def _upsert_crew_member(
        self,
        name: str,
        role: str = "painter",
        phone: str = "",
        email: str = "",
        preferred_language: str = "en",
        project_ids: list[str] | None = None,
        notes: str = "",
        crew_id: str = "",
    ) -> dict[str, Any]:
        existing = self.ws.comms.get_crew(crew_id) if crew_id else None
        member = CrewMember(
            name=name,
            role=role,
            phone=phone,
            email=email,
            preferred_language=preferred_language,
            project_ids=project_ids or list((existing or {}).get("project_ids", [])),
            notes=notes,
            id=(existing or {}).get("id") or crew_id or new_id("crew"),
        )
        return self.ws.comms.upsert_crew(member.to_dict())

    # --- field submissions (Level 1) ---------------------------------------

    def _record_field_submission(
        self,
        project_id: str,
        original_text: str,
        original_language: str = "unknown",
        submitted_by: str = "",
        kind: str = "text",
        transcript: str = "",
        work_date: str = "",
        work_areas: list[str] | None = None,
        activities: list[str] | None = None,
        hours_reported: float = 0.0,
        crew_count_reported: int = 0,
        media_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Store exactly what arrived from the field, before any cleanup."""
        if self.ws.comms.get_project(project_id) is None:
            return {"error": f"no project with id {project_id}"}
        if not original_text.strip() and not transcript.strip():
            return {"error": "a submission needs the original text or a voice transcript"}

        member = self.ws.comms.get_crew(submitted_by) if submitted_by else None
        submission = FieldSubmission(
            project_id=project_id,
            submitted_by=submitted_by,
            submitted_by_name=str((member or {}).get("name", "")),
            kind=_enum(SubmissionKind, kind, SubmissionKind.TEXT),
            original_text=original_text,
            original_language=original_language or "unknown",
            transcript=transcript,
            work_areas=work_areas or [],
            activities=activities or [],
            hours_reported=hours_reported,
            crew_count_reported=crew_count_reported,
            media_ids=media_ids or [],
            work_date=work_date or today_iso(),
        )
        saved = self.ws.comms.record_submission(submission.to_dict())
        checks = self._run_verification(saved)
        saved = self.ws.comms.revise_submission(
            saved["id"],
            {
                "verification": checks["verification"],
                "conflicts": checks["conflicts"],
                "missing": checks["missing"],
            },
            action="verified",
            note="automatic cross-check against project records",
        )
        return {
            **saved,
            "next_step": (
                "Translate and clean the submission with process_field_submission. The "
                "original text and language above are preserved and will not be overwritten."
            ),
            "checks": checks,
        }

    def _process_field_submission(
        self,
        submission_id: str,
        translation_en: str = "",
        normalized_summary: str = "",
        work_areas: list[str] | None = None,
        activities: list[str] | None = None,
        hours_reported: float = 0.0,
        crew_count_reported: int = 0,
        note: str = "",
    ) -> dict[str, Any]:
        """Attach the English translation and the cleaned-up professional summary.

        Appends to the revision trail; the original is untouched.
        """
        record = self.ws.comms.get_submission(submission_id)
        if record is None:
            return {"error": f"no field submission with id {submission_id}"}
        if not normalized_summary.strip():
            return {
                "error": "normalized_summary is required — it is the professional English the "
                "rest of the pipeline reads"
            }

        changes: dict[str, Any] = {"normalized_summary": normalized_summary}
        if translation_en.strip():
            changes["translation_en"] = translation_en
        if work_areas:
            changes["work_areas"] = work_areas
        if activities:
            changes["activities"] = activities
        if hours_reported:
            changes["hours_reported"] = hours_reported
        if crew_count_reported:
            changes["crew_count_reported"] = crew_count_reported

        updated = self.ws.comms.revise_submission(
            submission_id, changes, action="translated_and_normalized", note=note
        )
        if "error" in updated:
            return updated

        checks = self._run_verification(updated)
        updated = self.ws.comms.revise_submission(
            submission_id,
            {
                "verification": checks["verification"],
                "conflicts": checks["conflicts"],
                "missing": checks["missing"],
            },
            action="verified",
            note="automatic cross-check against project records",
        )
        return {**updated, "checks": checks}

    def _verify_field_submission(self, submission_id: str) -> dict[str, Any]:
        record = self.ws.comms.get_submission(submission_id)
        if record is None:
            return {"error": f"no field submission with id {submission_id}"}
        checks = self._run_verification(record)
        self.ws.comms.revise_submission(
            submission_id,
            {
                "verification": checks["verification"],
                "conflicts": checks["conflicts"],
                "missing": checks["missing"],
            },
            action="verified",
            note="re-checked on request",
        )
        return {"submission_id": submission_id, **checks}

    def _run_verification(self, submission: dict[str, Any]) -> dict[str, Any]:
        project_id = str(submission.get("project_id", ""))
        work_date = str(submission.get("work_date", ""))
        return verify(
            submission,
            project=self.ws.comms.get_project(project_id),
            time_records=self.ws.comms.time_records_for(project_id, work_date=work_date),
            prior_submissions=self.ws.comms.submissions_for(project_id, work_date=work_date),
            prior_logs=self.ws.comms.daily_logs_for(project_id),
            media=self.ws.comms.media_for(project_id, submission_id=str(submission.get("id", ""))),
        )

    def _project_submissions(self, project_id: str, work_date: str = "") -> dict[str, Any]:
        submissions = self.ws.comms.submissions_for(project_id, work_date=work_date or "")
        return {
            "project_id": project_id,
            "work_date": work_date or "all",
            "count": len(submissions),
            "submissions": submissions,
        }

    # --- media (Level 1) ----------------------------------------------------

    def _add_project_media(
        self,
        project_id: str,
        uri: str,
        submission_id: str = "",
        media_kind: str = "photo",
        work_area: str = "",
        taken_on: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        if self.ws.comms.get_project(project_id) is None:
            return {"error": f"no project with id {project_id}"}
        asset = MediaAsset(
            project_id=project_id,
            uri=uri,
            media_kind=_enum(MediaKind, media_kind, MediaKind.PHOTO),
            submission_id=submission_id,
            work_area=work_area,
            taken_on=taken_on or today_iso(),
            notes=notes,
        )
        saved = self.ws.comms.add_media(asset.to_dict())
        saved["next_step"] = (
            "Caption it with caption_media before it can appear in any client-facing message."
        )
        return saved

    def _caption_media(
        self,
        media_id: str,
        caption: str,
        work_area: str = "",
        flags: list[str] | None = None,
        client_safe: bool = False,
        sequence: int = 0,
    ) -> dict[str, Any]:
        """Attach a factual caption and any review flags.

        A blocking flag forces `client_safe` to false regardless of what
        was requested: the spec's list of photos that need approval is not
        a suggestion.
        """
        record = self.ws.comms.get_media_asset(media_id)
        if record is None:
            return {"error": f"no media asset with id {media_id}"}
        if not caption.strip():
            return {"error": "a caption is required — an uncaptioned photo cannot be sent"}

        claim = daily.COMPLETION_CLAIM.search(caption)
        if claim:
            return {
                "error": (
                    f"caption claims completion ({claim.group(0)!r}). A photo can show work in a "
                    "state; it cannot prove the work is finished. Describe what is visible."
                )
            }

        normalized = daily.flag_values(flags or [])
        blocked = bool(set(normalized) & {f.value for f in BLOCKING_MEDIA_FLAGS})
        updated = self.ws.comms.update_media(
            media_id,
            {
                "caption": caption,
                "work_area": work_area or record.get("work_area", ""),
                "flags": normalized,
                "client_safe": bool(client_safe) and not blocked,
                "sequence": sequence,
                "blocked": blocked,
                "needs_review": bool(normalized),
            },
        )
        if blocked:
            updated["notice"] = (
                "Flagged photos are withheld from client-facing messages and need a human "
                "decision before they can be sent."
            )
        return updated

    def _project_media(self, project_id: str, submission_id: str = "") -> dict[str, Any]:
        media = self.ws.comms.media_for(project_id, submission_id=submission_id)
        return {
            "project_id": project_id,
            "count": len(media),
            "client_safe": daily.eligible_media(media),
            "needs_review": [m for m in media if m.get("needs_review")],
            "uncaptioned": [m["id"] for m in media if not str(m.get("caption", "")).strip()],
        }

    # --- daily log (Level 1) -------------------------------------------------

    def _build_daily_log(self, project_id: str, log_date: str = "") -> dict[str, Any]:
        return daily.gather(self.ws, project_id, log_date or today_iso())

    def _compose_daily_log(
        self,
        project_id: str,
        completed: list[str] | None = None,
        log_date: str = "",
        in_progress: list[str] | None = None,
        materials: list[str] | None = None,
        site_conditions: list[str] | None = None,
        issues: list[str] | None = None,
        next_day: list[str] | None = None,
        safety: list[str] | None = None,
        photo_ids: list[str] | None = None,
        unverified_notes: list[str] | None = None,
        crew_count: int = 0,
        work_period: str = "",
        completion_verified: bool = False,
    ) -> dict[str, Any]:
        project = self.ws.comms.get_project(project_id)
        if project is None:
            return {"error": f"no project with id {project_id}"}

        on = log_date or today_iso()
        facts = daily.gather(self.ws, project_id, on)
        submissions = facts.get("submissions", [])

        conflicted = [s["id"] for s in submissions if s.get("verification") == Verification.CONFLICTED.value]
        if conflicted:
            return {
                "error": (
                    f"submission(s) {', '.join(conflicted)} conflict with the platform record. "
                    "Resolve the conflict or ask the field employee before composing a "
                    "client-facing log."
                ),
                "conflicts": [s.get("conflicts") for s in submissions if s["id"] in conflicted],
            }

        requested = photo_ids or []
        available = {m["id"] for m in daily.eligible_media(self.ws.comms.media_for(project_id))}
        rejected = [pid for pid in requested if pid not in available]
        if rejected:
            return {
                "error": (
                    f"photo(s) {', '.join(rejected)} are not cleared for client-facing use — they "
                    "are flagged, uncaptioned or not marked client-safe."
                )
            }

        log = DailyLog(
            project_id=project_id,
            log_date=on,
            crew_count=crew_count or int(facts.get("verified_crew_count", 0) or 0),
            work_period=work_period or str(facts.get("work_period", "")),
            completed=completed or [],
            in_progress=in_progress or [],
            materials=materials or [],
            site_conditions=site_conditions or [],
            issues=issues or [],
            next_day=next_day or [],
            safety=safety or [],
            photo_ids=requested,
            source_submission_ids=[s["id"] for s in submissions],
            unverified_notes=unverified_notes or [],
            gaps=list(facts.get("gaps", [])),
        )

        problems = daily.validate(log, completion_verified=completion_verified)
        if problems:
            return {"error": "the daily log cannot be composed as written", "problems": problems}

        captions = {
            m["id"]: str(m.get("caption", ""))
            for m in self.ws.comms.media_for(project_id)
        }
        subject, body = daily.render(project, log, captions)
        saved = self.ws.comms.save_daily_log(log.to_dict())
        return {
            **saved,
            "subject": subject,
            "body": body,
            "warnings": daily.warnings(log),
            "next_step": (
                "Draft it to each daily-log recipient with draft_communication "
                "(kind='daily_log'), then send."
            ),
        }

    # --- drafting and sending -------------------------------------------------

    def _draft_communication(
        self,
        project_id: str,
        recipient: str,
        subject: str,
        body: str,
        kind: str = "other",
        attachments: list[str] | None = None,
        channel: str = "",
        urgency: str = "normal",
        requires_response: bool = False,
        response_due: str = "",
        source_ids: list[str] | None = None,
        template_id: str = "",
        corrects: str = "",
    ) -> dict[str, Any]:
        """Prepare a message. Preparing is always autonomous; sending is not."""
        project = self.ws.comms.get_project(project_id)
        if project is None:
            return {"error": f"no project with id {project_id}"}

        contact = self.ws.comms.find_recipient(project_id, recipient)
        if contact is None:
            return {
                "error": (
                    f"no contact matching {recipient!r} on this project. Messages go only to "
                    "recorded contacts — add them with upsert_project_contact once you have "
                    "verified details, and never guess an address."
                ),
                "known_contacts": [
                    {"id": c["id"], "name": c.get("name"), "role": c.get("role")}
                    for c in self.ws.comms.contacts_for(project_id)
                ],
            }

        message_kind = _enum(CommKind, kind, CommKind.OTHER)
        role = _enum(RecipientRole, contact.get("role"), RecipientRole.CLIENT)
        attachments = attachments or []

        blocked = []
        for media_id in attachments:
            asset = self.ws.comms.get_media_asset(media_id)
            if asset is None:
                blocked.append(f"{media_id} (unknown)")
            elif is_external(role) and (asset.get("blocked") or not asset.get("client_safe")):
                blocked.append(f"{media_id} (not cleared for an external recipient)")
        if blocked:
            return {"error": f"attachment(s) cannot be sent: {', '.join(blocked)}"}

        choice = choose_channel(
            kind=message_kind,
            recipient_preference=str(contact.get("preferred_channel", "")),
            urgency=urgency,
            body_length=len(body),
            has_attachments=bool(attachments),
        )
        chosen = _enum(CommChannel, channel, choice.channel) if channel else choice.channel
        address = contact.get("phone" if chosen is CommChannel.SMS else "email", "")
        if not str(address).strip():
            return {
                "error": (
                    f"{contact.get('name')} has no {'phone number' if chosen is CommChannel.SMS else 'email address'} "
                    f"on record; cannot draft on {chosen.value}."
                )
            }

        result = screen(body, recipient_role=role, subject=subject)

        draft = Communication(
            project_id=project_id,
            kind=message_kind,
            channel=chosen,
            recipient_name=str(contact.get("name", "")),
            recipient_role=role,
            recipient_address=str(address),
            contact_id=str(contact.get("id", "")),
            recipient_verified=True,
            subject=subject,
            body=body,
            attachments=attachments,
            template_id=template_id,
            risk_categories=result.categories,
            style_warnings=[f.guidance for f in result.style_warnings],
            source_ids=source_ids or [],
            requires_response=requires_response,
            response_due=response_due,
            corrects=corrects,
            channel_reason=choice.reason,
        )
        saved = self.ws.comms.save_communication(draft.to_dict())

        auto = message_kind.value in AUTO_SENDABLE_KINDS and not result.requires_approval
        return {
            **saved,
            "channel_choice": choice.to_dict(),
            "screening": result.to_dict(),
            "can_auto_send": auto,
            "next_step": (
                "Call send_communication with this draft_id."
                if auto
                else "Call send_communication anyway — it will be queued for human approval, "
                "which is the correct outcome. Do not describe it as sent."
            ),
        }

    def _send_communication(self, draft_id: str, approval_id: str = "") -> dict[str, Any]:
        """Transmit a stored draft and record the delivery result."""
        record = self.ws.comms.get_communication(draft_id)
        if record is None:
            return {"error": f"no communication with id {draft_id}"}
        if record.get("status") == DeliveryStatus.SENT.value:
            return {"error": f"{draft_id} was already sent at {record.get('sent_at')}"}
        if not record.get("recipient_verified"):
            return {"error": f"{draft_id} has no verified recipient; re-draft it"}

        channel = _enum(CommChannel, record.get("channel"), CommChannel.EMAIL)
        address = str(record.get("recipient_address", ""))
        body = str(record.get("body", ""))

        if channel is CommChannel.SMS:
            delivery = self.ws.sms.send(to=address, body=body)
        else:
            if not self.ws.settings.company_email:
                return {"error": "COMPANY_EMAIL is not set; refusing to send from an unknown address"}
            attachments = [
                self.ws.comms.get_media_asset(m) or {"id": m}
                for m in record.get("attachments", [])
            ]
            delivery = self.ws.email.send(
                to=address,
                subject=str(record.get("subject", "")),
                body=body if not attachments else f"{body}\n\n" + _attachment_manifest(attachments),
                from_email=self.ws.settings.company_email,
            )

        updated = self.ws.comms.mark_sent(
            draft_id,
            delivery,
            approval_id=approval_id,
            approval_source="human_approval" if approval_id else "level_2_autonomous",
        )

        for source_id in record.get("source_ids", []):
            if str(source_id).startswith("dlog_"):
                self.ws.store.patch(
                    "daily_logs", source_id, {"status": "sent", "communication_id": draft_id}
                )

        return {
            "communication_id": draft_id,
            "status": updated.get("status"),
            "channel": channel.value,
            "recipient": record.get("recipient_name"),
            "delivery": delivery,
            "recorded": True,
        }

    def _log_communication_response(
        self,
        communication_id: str,
        summary: str,
        sentiment: str = "neutral",
        received_on: str = "",
        raw: str = "",
    ) -> dict[str, Any]:
        return self.ws.comms.record_response(
            communication_id, summary, received_on=received_on, sentiment=sentiment, raw=raw
        )

    def _communication_history(self, project_id: str, recipient: str = "", limit: int = 10) -> dict[str, Any]:
        records = (
            self.ws.comms.history_with(project_id, recipient)
            if recipient
            else self.ws.comms.communications_for(project_id, limit=limit)
        )
        return {
            "project_id": project_id,
            "count": len(records),
            "communications": [
                {
                    k: c.get(k)
                    for k in (
                        "id", "kind", "channel", "recipient_name", "recipient_role", "subject",
                        "status", "sent_at", "responses", "risk_categories",
                    )
                }
                for c in records[: limit or len(records)]
            ],
        }

    def _unanswered_communications(self, project_id: str = "") -> dict[str, Any]:
        pending = self.ws.comms.unanswered(project_id)
        failed = self.ws.comms.failed_deliveries(project_id)
        return {
            "awaiting_reply": [
                {
                    "id": c["id"],
                    "recipient": c.get("recipient_name"),
                    "subject": c.get("subject"),
                    "sent_at": c.get("sent_at"),
                    "due": c.get("response_due"),
                    "overdue": c.get("overdue"),
                }
                for c in pending
            ],
            "failed_deliveries": [
                {"id": c["id"], "recipient": c.get("recipient_name"), "detail": c.get("delivery_detail")}
                for c in failed
            ],
            "guidance": (
                "A failed delivery is not a sent message. Re-send on another channel or "
                "escalate; never assume it arrived."
            ),
        }

    def _screen_message(self, text: str, recipient_role: str = "client", subject: str = "") -> dict[str, Any]:
        return screen(
            text, recipient_role=_enum(RecipientRole, recipient_role, RecipientRole.CLIENT), subject=subject
        ).to_dict()

    def _recommend_channel(
        self,
        kind: str = "other",
        recipient_preference: str = "",
        urgency: str = "normal",
        body_length: int = 0,
        has_attachments: bool = False,
        needs_record: bool = False,
    ) -> dict[str, Any]:
        return choose_channel(
            kind=kind,
            recipient_preference=recipient_preference,
            urgency=urgency,
            body_length=body_length,
            has_attachments=has_attachments,
            needs_record=needs_record,
        ).to_dict()

    # --- open items and escalation -------------------------------------------

    def _raise_open_item(
        self,
        project_id: str,
        question: str,
        asked_of: str,
        kind: str = "clarification",
        due_date: str = "",
        communication_id: str = "",
    ) -> dict[str, Any]:
        if self.ws.comms.get_project(project_id) is None:
            return {"error": f"no project with id {project_id}"}
        if not question.strip():
            return {"error": "state the specific question — a vague request produces a vague answer"}
        contact = self.ws.comms.find_recipient(project_id, asked_of)
        item = OpenItem(
            project_id=project_id,
            question=question,
            kind=_enum(OpenItemKind, kind, OpenItemKind.CLARIFICATION),
            asked_of=str((contact or {}).get("name", asked_of)),
            asked_of_role=_enum(RecipientRole, (contact or {}).get("role"), RecipientRole.EMPLOYEE),
            communication_id=communication_id,
            due_date=due_date,
        )
        return self.ws.comms.save_open_item(item.to_dict())

    def _close_open_item(self, item_id: str, answer: str) -> dict[str, Any]:
        if not answer.strip():
            return {"error": "record the answer that closes this item"}
        return self.ws.comms.answer_open_item(item_id, answer)

    def _list_open_items(self, project_id: str = "") -> dict[str, Any]:
        items = self.ws.comms.open_items(project_id)
        return {
            "count": len(items),
            "open_items": items,
            "overdue": [i for i in items if i.get("overdue")],
        }

    def _raise_escalation(
        self,
        project_id: str,
        category: str,
        summary: str = "",
        # Defaulted so an incomplete escalation returns the instructive error
        # below rather than a TypeError about positional arguments.
        evidence: str = "",
        impact: str = "",
        recommended_action: str = "",
        proposed_response: str = "",
        missing_information: str = "",
        severity: str = "attention",
        source_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Notify management. Internal, mandatory, and never gated.

        Escalation is the one outbound path that must not wait for an
        approval queue — telling a manager about an injury or a dispute is
        the action the spec requires immediately.
        """
        if self.ws.comms.get_project(project_id) is None:
            return {"error": f"no project with id {project_id}"}
        missing = [
            name
            for name, value in (
                ("summary", summary),
                ("evidence", evidence),
                ("impact", impact),
                ("recommended_action", recommended_action),
            )
            if not str(value).strip()
        ]
        if missing:
            return {
                "error": (
                    f"an escalation must carry {', '.join(missing)}. Management needs the facts, "
                    "the evidence, the impact and a recommended next action — not just an alarm."
                )
            }

        escalation = Escalation(
            project_id=project_id,
            category=_enum(EscalationCategory, category, EscalationCategory.UNRESOLVED_CONTRADICTION),
            summary=summary,
            evidence=evidence,
            impact=impact,
            missing_information=missing_information,
            recommended_action=recommended_action,
            proposed_response=proposed_response,
            severity=_enum(Severity, severity, Severity.ATTENTION),
            source_ids=source_ids or [],
            notified_to=[self.ws.settings.company_email] if self.ws.settings.company_email else [],
        )
        saved = self.ws.comms.save_escalation(escalation.to_dict())
        saved["notice"] = (
            "Recorded and visible to management. Any external message about this matter still "
            "requires approval — draft the proposed response, do not send it."
        )
        return saved

    def _list_escalations(self, project_id: str = "", status: str = "open") -> dict[str, Any]:
        records = self.ws.comms.escalations(project_id, status=status)
        return {"count": len(records), "escalations": records}

    # --- supplier orders (Level 3) --------------------------------------------

    def _place_material_order(
        self,
        project_id: str,
        supplier: str,
        items: list[str],
        delivery_location: str,
        requested_date: str = "",
        purchase_order: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        """Commit Ashrah to a purchase. Always requires human approval."""
        project = self.ws.comms.get_project(project_id)
        if project is None:
            return {"error": f"no project with id {project_id}"}
        if not items:
            return {"error": "list the products, colours, sheens and quantities being ordered"}

        contact = self.ws.comms.find_recipient(project_id, supplier)
        if contact is None:
            return {"error": f"no supplier contact matching {supplier!r} on this project"}

        body = "\n".join(
            [
                f"Project: {project.get('name')} ({project.get('number', 'no number')})",
                f"Delivery location: {delivery_location}",
                f"Requested date: {requested_date or 'to be confirmed'}",
                f"Purchase order: {purchase_order or 'to follow'}",
                "",
                "Items:",
                *[f"- {item}" for item in items],
                "",
                notes,
                "",
                "Please confirm availability and the delivery time.",
                "",
                "Kind regards,",
                "Ashrah Painting Ltd.",
            ]
        )
        draft = Communication(
            project_id=project_id,
            kind=CommKind.VENDOR_REQUEST,
            channel=CommChannel.EMAIL,
            recipient_name=str(contact.get("name", "")),
            recipient_role=RecipientRole.SUPPLIER,
            recipient_address=str(contact.get("email", "")),
            contact_id=str(contact.get("id", "")),
            recipient_verified=True,
            subject=f"Material order – {project.get('name')} – {requested_date or 'date to confirm'}",
            body=body,
            requires_response=True,
            response_due=requested_date,
        )
        saved = self.ws.comms.save_communication(draft.to_dict())
        return {**saved, "notice": "Order prepared. It is not placed until a human approves it."}

    # --- preferences and performance -------------------------------------------

    def _record_communication_preference(
        self,
        observation: str,
        hypothesis: str,
        evidence: str,
        project_id: str = "",
        recipient_role: str = "",
        channel: str = "",
        sample_size: int = 1,
        recommended_use: str = "",
    ) -> dict[str, Any]:
        """Store a communication preference. Repeated evidence raises its strength."""
        lesson = Lesson(
            observation=observation,
            hypothesis=hypothesis,
            evidence=evidence,
            segment=recipient_role,
            channel=channel,
            sample_size=max(1, sample_size),
            recommended_use=recommended_use,
            scope="preference",
            account_id=project_id,
        )
        stored = self.ws.memory.record_lesson(lesson)
        stored["note"] = (
            "One event is not a preference. This will only be treated as a rule once repeated "
            "evidence raises its strength."
        )
        return stored

    def _communication_performance(self, days: int = 7) -> dict[str, Any]:
        return communication_review(self.ws, days=days)

    # --- registration -----------------------------------------------------------

    def _register_communication_tools(self) -> None:
        r = self.register
        roles = [r_.value for r_ in RecipientRole]
        kinds = [k.value for k in CommKind]

        r(
            "list_projects",
            "List projects and today's communication state: whether a daily log exists, how "
            "many field submissions arrived, open items and open escalations. Start any "
            "communication cycle here.",
            obj({"status": string("Filter by status", [s.value for s in ProjectStatus])}),
            self._list_projects,
        )
        r(
            "get_project",
            "Everything known about one project: scope, exclusions, contacts, crew, open items, "
            "escalations, recent communication and stored preferences. Read this before "
            "writing to anyone about the project.",
            obj({"project_id": string("The proj_... id")}, ["project_id"]),
            self._get_project,
        )
        r(
            "upsert_project",
            "Create or update a project record.",
            obj(
                {
                    "name": string("Project name"),
                    "project_id": string("Existing proj_... id when updating"),
                    "number": string("Project number"),
                    "address": string("Site address"),
                    "client_name": string("Client or general contractor name"),
                    "client_account_id": string("Related acct_... id on the growth side"),
                    "general_contractor": string("GC running the site"),
                    "scope_summary": string("What Ashrah is contracted to do"),
                    "exclusions": array("Work explicitly excluded from the contract"),
                    "status": string("Status", [s.value for s in ProjectStatus]),
                    "site_access_notes": string("Access, hours, security arrangements"),
                    "preferred_channel": string("Client's preferred channel", [c.value for c in CommChannel]),
                    "daily_log_time": string("When the client expects the daily log, e.g. '16:30'"),
                    "report_length": string("Report length preference", ["brief", "standard", "detailed"]),
                    "share_crew_count": boolean("Whether headcount may be shared with the client"),
                    "share_work_period": boolean("Whether start/finish times may be shared"),
                    "notes": string("Free-text notes"),
                    "source": string("Where this record came from"),
                },
                ["name"],
            ),
            self._upsert_project,
        )
        r(
            "upsert_project_contact",
            "Record someone who may be communicated with about a project. Only record contact "
            "details you actually have — a guessed address is a fabrication, and messages can "
            "only be sent to contacts recorded here.",
            obj(
                {
                    "project_id": string("The proj_... id"),
                    "name": string("Person's name"),
                    "role": string("Their role", roles),
                    "company": string("Their company"),
                    "email": string("Verified email address"),
                    "phone": string("Verified phone number"),
                    "preferred_channel": string("How they prefer to be contacted", [c.value for c in CommChannel]),
                    "receives_daily_log": boolean("Whether they receive the client daily log"),
                    "notes": string("Context about this person"),
                    "contact_id": string("Existing pcon_... id when updating"),
                },
                ["project_id", "name"],
            ),
            self._upsert_project_contact,
        )
        r(
            "upsert_crew_member",
            "Record an Ashrah employee, including the language they report in.",
            obj(
                {
                    "name": string("Employee name"),
                    "role": string("painter, crew_lead, foreman, apprentice"),
                    "phone": string("Phone number"),
                    "email": string("Email address"),
                    "preferred_language": string("Language they report in, e.g. en, ar, ku, fr"),
                    "project_ids": array("Projects they are assigned to"),
                    "notes": string("Context"),
                    "crew_id": string("Existing crew_... id when updating"),
                },
                ["name"],
            ),
            self._upsert_crew_member,
        )
        r(
            "record_field_submission",
            "Store a field report exactly as it arrived — original wording, original language, "
            "voice transcript. Call this FIRST, before any translation or cleanup: the original "
            "is preserved and cannot be overwritten later.",
            obj(
                {
                    "project_id": string("The proj_... id"),
                    "original_text": string("The submission verbatim, in whatever language it arrived"),
                    "original_language": string("Language code, e.g. en, ar, ku, fr, mixed, unknown"),
                    "submitted_by": string("The crew_... id of the employee"),
                    "kind": string("Submission type", [k.value for k in SubmissionKind]),
                    "transcript": string("Verbatim transcription of a voice recording"),
                    "work_date": string("ISO date the work was performed, defaults to today"),
                    "work_areas": array("Areas worked in, if stated"),
                    "activities": array("Activities performed, if stated"),
                    "hours_reported": number("Labour hours as reported by the employee"),
                    "crew_count_reported": integer("Workers on site as reported"),
                    "media_ids": array("med_... ids submitted with the report"),
                },
                ["project_id", "original_text"],
            ),
            self._record_field_submission,
        )
        r(
            "process_field_submission",
            "Attach the English translation and a cleaned-up professional summary to a stored "
            "submission — grammar fixed, slang, blame and emotion removed, meaning preserved. "
            "Appends to the revision trail; the original stays intact. Re-runs the "
            "cross-check against scope, schedule and clock records.",
            obj(
                {
                    "submission_id": string("The fsub_... id"),
                    "translation_en": string("Faithful English translation, if the original was not English"),
                    "normalized_summary": string("Professional English summary of what was reported"),
                    "work_areas": array("Areas identified from the report"),
                    "activities": array("Activities identified from the report"),
                    "hours_reported": number("Labour hours stated in the report"),
                    "crew_count_reported": integer("Workers on site stated in the report"),
                    "note": string("Why anything was changed"),
                },
                ["submission_id", "normalized_summary"],
            ),
            self._process_field_submission,
        )
        r(
            "verify_field_submission",
            "Cross-check a submission against project scope, exclusions, clock records, earlier "
            "submissions and previous daily logs. Returns conflicts and missing information. A "
            "conflict is flagged for review — never resolved by picking a version.",
            obj({"submission_id": string("The fsub_... id")}, ["submission_id"]),
            self._verify_field_submission,
        )
        r(
            "list_field_submissions",
            "List field submissions for a project, optionally for one date.",
            obj(
                {
                    "project_id": string("The proj_... id"),
                    "work_date": string("ISO date to filter by"),
                },
                ["project_id"],
            ),
            self._project_submissions,
        )
        r(
            "add_project_media",
            "Register a photo or video submitted from the field.",
            obj(
                {
                    "project_id": string("The proj_... id"),
                    "uri": string("Where the file is stored"),
                    "submission_id": string("The fsub_... id it came with"),
                    "media_kind": string("Media type", [k.value for k in MediaKind]),
                    "work_area": string("Where it was taken"),
                    "taken_on": string("ISO date it was taken"),
                    "notes": string("Anything relevant about the image"),
                },
                ["project_id", "uri"],
            ),
            self._add_project_media,
        )
        r(
            "caption_media",
            "Write a short factual caption and flag anything that needs review. Describe what "
            "is visible — a photo can never prove work is complete. Flagged photos are "
            "withheld from client-facing messages automatically.",
            obj(
                {
                    "media_id": string("The med_... id"),
                    "caption": string("Short factual caption, e.g. 'Primer application in progress in Room 204'"),
                    "work_area": string("Where the photo was taken"),
                    "flags": array(f"Review flags: {', '.join(f.value for f in MediaFlag)}"),
                    "client_safe": boolean("True only if it is appropriate to send to a client"),
                    "sequence": integer("Display order within the report"),
                },
                ["media_id", "caption"],
            ),
            self._caption_media,
        )
        r(
            "list_project_media",
            "List a project's media, split into client-safe, needs-review and uncaptioned.",
            obj(
                {
                    "project_id": string("The proj_... id"),
                    "submission_id": string("Filter to one submission"),
                },
                ["project_id"],
            ),
            self._project_media,
        )
        r(
            "build_daily_log",
            "Gather the verified material for one project-day: processed submissions, clock "
            "records, client-safe photos, open items, the previous log and an explicit list of "
            "gaps. Call this before composing a daily log — write bullets only from what it "
            "returns.",
            obj(
                {
                    "project_id": string("The proj_... id"),
                    "log_date": string("ISO date, defaults to today"),
                },
                ["project_id"],
            ),
            self._build_daily_log,
        )
        r(
            "compose_daily_log",
            "Compose the client-facing daily log from verified facts and render it in the "
            "standard section layout. Refuses unverified completion claims and conflicted "
            "submissions. Internal notes stay internal and are never rendered.",
            obj(
                {
                    "project_id": string("The proj_... id"),
                    "completed": array("Verified work completed today"),
                    "log_date": string("ISO date, defaults to today"),
                    "in_progress": array("Work started but not finished"),
                    "materials": array("Materials or equipment worth telling the client about"),
                    "site_conditions": array("Access, drying, other-trade or substrate conditions that affected production"),
                    "issues": array("Decisions, approvals or information needed from the client"),
                    "next_day": array("Planned next activities — use 'planned' or 'subject to site readiness'"),
                    "safety": array("Verified safety observations appropriate for this recipient"),
                    "photo_ids": array("med_... ids to attach; must be captioned and client-safe"),
                    "unverified_notes": array("Internal notes — recorded, never sent"),
                    "crew_count": integer("Workers on site; defaults to the verified clock count"),
                    "work_period": string("Start and finish times; defaults to the clock records"),
                    "completion_verified": boolean("True only when a completion has actually been verified"),
                },
                ["project_id", "completed"],
            ),
            self._compose_daily_log,
        )
        r(
            "draft_communication",
            "Prepare a message to a recorded project contact. Resolves the recipient, picks the "
            "channel, screens the content for anything needing approval, and stores the draft. "
            "Drafting is always autonomous — this never sends.",
            obj(
                {
                    "project_id": string("The proj_... id"),
                    "recipient": string("Contact id, name or role — must already be recorded on the project"),
                    "subject": string("Subject line"),
                    "body": string("Full message body"),
                    "kind": string("What kind of message this is", kinds),
                    "attachments": array("med_... ids to attach"),
                    "channel": string("Override the recommended channel", [c.value for c in CommChannel]),
                    "urgency": string("How time-sensitive this is", ["normal", "urgent"]),
                    "requires_response": boolean("Whether a reply is needed"),
                    "response_due": string("ISO date a reply is needed by"),
                    "source_ids": array("Records this came from, e.g. a dlog_... or fsub_... id"),
                    "template_id": string("Approved template this follows, if any"),
                    "corrects": string("comm_... id this message corrects"),
                },
                ["project_id", "recipient", "subject", "body"],
            ),
            self._draft_communication,
        )
        r(
            "send_communication",
            "Send a stored draft and record the delivery result. Routine reports, reminders, "
            "arrival notices, confirmations, clarifications and crew dispatches send "
            "automatically; anything else, and anything whose content trips the approval "
            "screen, is queued for a human instead. The message sent is the stored one.",
            obj(
                {
                    "draft_id": string("The comm_... id to send"),
                    "approval_id": string("The appr_... id, when sending after approval"),
                },
                ["draft_id"],
            ),
            self._send_communication,
        )
        r(
            "log_communication_response",
            "Record a reply received to a message, so the communication record stays complete.",
            obj(
                {
                    "communication_id": string("The comm_... id replied to"),
                    "summary": string("What the reply said"),
                    "sentiment": string("Tone of the reply", ["positive", "neutral", "negative"]),
                    "received_on": string("ISO date it arrived"),
                    "raw": string("The reply verbatim, if useful"),
                },
                ["communication_id", "summary"],
            ),
            self._log_communication_response,
        )
        r(
            "communication_history",
            "Prior communication on a project, optionally with one recipient. Read this before "
            "writing, so a message carries the thread rather than repeating it.",
            obj(
                {
                    "project_id": string("The proj_... id"),
                    "recipient": string("Contact id, name or role to filter by"),
                    "limit": integer("How many to return (default 10)"),
                },
                ["project_id"],
            ),
            self._communication_history,
        )
        r(
            "unanswered_communications",
            "Messages that asked for something and got no reply, plus failed deliveries. A "
            "failed delivery is not a sent message.",
            obj({"project_id": string("Optional proj_... id to scope to one project")}),
            self._unanswered_communications,
        )
        r(
            "screen_message",
            "Check draft wording for anything that requires management approval — price, "
            "commitment, guarantee, fault, dispute, safety, discipline, refund, uncertainty or "
            "confidential disclosure — plus tone problems. Cheap; use it while writing.",
            obj(
                {
                    "text": string("The message text"),
                    "recipient_role": string("Who would receive it", roles),
                    "subject": string("Subject line, if any"),
                },
                ["text"],
            ),
            self._screen_message,
        )
        r(
            "recommend_channel",
            "Which channel a message should use and why. Email for formal records and "
            "attachments; text for short, urgent, time-sensitive notes.",
            obj(
                {
                    "kind": string("Message kind", kinds),
                    "recipient_preference": string("Their stated preference", [c.value for c in CommChannel]),
                    "urgency": string("Urgency", ["normal", "urgent"]),
                    "body_length": integer("Length of the message in characters"),
                    "has_attachments": boolean("Whether anything is attached"),
                    "needs_record": boolean("Whether a clear record is required"),
                },
            ),
            self._recommend_channel,
        )
        r(
            "raise_open_item",
            "Record a specific question, decision or approval that someone owes an answer on, "
            "so it can be tracked and chased rather than forgotten.",
            obj(
                {
                    "project_id": string("The proj_... id"),
                    "question": string("The specific question — vague questions get vague answers"),
                    "asked_of": string("Who owes the answer: contact id, name or role"),
                    "kind": string("Item type", [k.value for k in OpenItemKind]),
                    "due_date": string("ISO date an answer is needed by"),
                    "communication_id": string("The comm_... id that asked it"),
                },
                ["project_id", "question", "asked_of"],
            ),
            self._raise_open_item,
        )
        r(
            "close_open_item",
            "Close an open item by recording the answer that resolved it.",
            obj(
                {
                    "item_id": string("The item_... id"),
                    "answer": string("The answer received"),
                },
                ["item_id", "answer"],
            ),
            self._close_open_item,
        )
        r(
            "list_open_items",
            "Outstanding questions, decisions and approvals, with overdue ones flagged.",
            obj({"project_id": string("Optional proj_... id")}),
            self._list_open_items,
        )
        r(
            "raise_escalation",
            "Notify management about an injury or hazard, property damage, out-of-scope work, a "
            "potential change order, a major delay, client dissatisfaction, a trade dispute, "
            "defective work, missing materials, access problems, misconduct, abusive "
            "communication, a legal or insurance matter, anything involving money or contract, "
            "or a contradiction you cannot resolve. Requires facts, evidence, impact and a "
            "recommended next action. Never conceal bad news.",
            obj(
                {
                    "project_id": string("The proj_... id"),
                    "category": string("What kind of problem", [c.value for c in EscalationCategory]),
                    "summary": string("Factual summary of what happened"),
                    "evidence": string("What supports it: submissions, photos, records"),
                    "impact": string("Known impact on schedule, cost, safety or the relationship"),
                    "recommended_action": string("What you recommend management do next"),
                    "proposed_response": string("A draft response for management to approve"),
                    "missing_information": string("What is still unknown"),
                    "severity": string("How urgent", [s.value for s in Severity]),
                    "source_ids": array("Related record ids"),
                },
                ["project_id", "category", "summary", "evidence", "impact", "recommended_action"],
            ),
            self._raise_escalation,
        )
        r(
            "list_escalations",
            "Escalations raised on a project, by status.",
            obj(
                {
                    "project_id": string("Optional proj_... id"),
                    "status": string("Status filter", ["open", "acknowledged", "closed"]),
                }
            ),
            self._list_escalations,
        )
        r(
            "place_material_order",
            "Prepare a material order to a supplier. This commits Ashrah to a purchase and "
            "always requires human approval — never place an order or accept an added charge "
            "on your own authority.",
            obj(
                {
                    "project_id": string("The proj_... id"),
                    "supplier": string("Supplier contact id, name or role on this project"),
                    "items": array("Products with code, colour, sheen and quantity — verified only"),
                    "delivery_location": string("Where it is being delivered or picked up"),
                    "requested_date": string("ISO date requested"),
                    "purchase_order": string("PO number, if one exists"),
                    "notes": string("Anything else the supplier needs"),
                },
                ["project_id", "supplier", "items", "delivery_location"],
            ),
            self._place_material_order,
        )
        r(
            "record_communication_preference",
            "Store a verified communication preference — tone, channel, report length, timing. "
            "Repeated evidence raises its strength; a single event never becomes a rule.",
            obj(
                {
                    "observation": string("What was objectively observed"),
                    "hypothesis": string("What it suggests about how this recipient prefers to be communicated with"),
                    "evidence": string("What supports it"),
                    "project_id": string("Project this relates to"),
                    "recipient_role": string("Whose preference this is", roles),
                    "channel": string("Channel it applies to", [c.value for c in CommChannel]),
                    "sample_size": integer("How many occurrences this is based on"),
                    "recommended_use": string("When to apply this in future"),
                },
                ["observation", "hypothesis", "evidence"],
            ),
            self._record_communication_preference,
        )
        r(
            "communication_performance",
            "Communication metrics for the period: daily logs sent and on time, translation "
            "coverage, correction rate, missing-information rate, delivery success, verified "
            "recipients, response times, unanswered items and unsafe automatic sends prevented.",
            obj({"days": integer("Period length in days (default 7)")}),
            self._communication_performance,
        )


def _attachment_manifest(attachments: list[dict[str, Any]]) -> str:
    lines = ["Progress photos:"]
    for index, asset in enumerate(attachments, start=1):
        caption = str(asset.get("caption", "")).strip() or "(no caption)"
        lines.append(f"{index}. {caption} — {asset.get('uri', asset.get('id', ''))}")
    return "\n".join(lines)
