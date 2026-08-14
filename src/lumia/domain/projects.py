"""Project-communication objects.

These are the nouns the Communication Agent works on. The growth side of
the platform reasons about accounts and pipeline; this side reasons about
what happened on a jobsite today, who needs to be told, and what the
record shows afterwards.

Two rules from the operating spec are enforced in the shape of the data
rather than left to the prompt:

1. **The original submission is never overwritten.** A `FieldSubmission`
   keeps the raw text, the language it arrived in, the transcript and the
   translation as separate fields, and every change appends to
   `revisions`. What was submitted, what was changed and why stays
   recoverable.
2. **A claim carries its verification state.** Anything the platform could
   not corroborate is `UNVERIFIED` or `CONFLICTED`, and those notes live
   in internal fields that the client-facing renderer does not read.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any

from ._serde import serialize


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def today_iso() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ProjectStatus(str, Enum):
    PLANNED = "planned"
    ACTIVE = "active"
    ON_HOLD = "on_hold"
    SUBSTANTIALLY_COMPLETE = "substantially_complete"
    COMPLETE = "complete"
    CLOSED = "closed"


#: Projects that still generate daily communication.
LIVE_STATUSES = {ProjectStatus.ACTIVE, ProjectStatus.SUBSTANTIALLY_COMPLETE}


class RecipientRole(str, Enum):
    """Who a message is going to. Drives tone, content and what stays private."""

    CLIENT = "client"
    GENERAL_CONTRACTOR = "general_contractor"
    PROJECT_MANAGER = "project_manager"
    SUPERINTENDENT = "superintendent"
    EMPLOYEE = "employee"
    CREW_LEAD = "crew_lead"
    SUBCONTRACTOR = "subcontractor"
    SUPPLIER = "supplier"
    MANAGEMENT = "management"


#: Recipients outside Ashrah. Internal financial, employee and management
#: information must never reach these without approval.
EXTERNAL_ROLES = {
    RecipientRole.CLIENT,
    RecipientRole.GENERAL_CONTRACTOR,
    RecipientRole.PROJECT_MANAGER,
    RecipientRole.SUPERINTENDENT,
    RecipientRole.SUBCONTRACTOR,
    RecipientRole.SUPPLIER,
}

#: Recipients who receive the client-facing daily log by default.
DAILY_LOG_ROLES = {
    RecipientRole.CLIENT,
    RecipientRole.GENERAL_CONTRACTOR,
    RecipientRole.PROJECT_MANAGER,
    RecipientRole.SUPERINTENDENT,
}


def is_external(role: RecipientRole | str) -> bool:
    try:
        return RecipientRole(role) in EXTERNAL_ROLES
    except ValueError:
        # An unrecognized role is treated as external: unknown means careful.
        return True


class CommChannel(str, Enum):
    EMAIL = "email"
    SMS = "sms"


class CommKind(str, Enum):
    """What kind of message this is. Decides whether it can ever auto-send."""

    DAILY_LOG = "daily_log"
    SCHEDULE_REMINDER = "schedule_reminder"
    ARRIVAL_NOTICE = "arrival_notice"
    CONFIRMATION_REQUEST = "confirmation_request"
    CLARIFICATION_REQUEST = "clarification_request"
    CREW_DISPATCH = "crew_dispatch"
    VENDOR_REQUEST = "vendor_request"
    CLIENT_UPDATE = "client_update"
    ESCALATION_NOTICE = "escalation_notice"
    COMMITMENT = "commitment"
    OTHER = "other"


class DeliveryStatus(str, Enum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    SENT = "sent"
    FAILED = "failed"
    REPLIED = "replied"
    CANCELLED = "cancelled"


class SubmissionKind(str, Enum):
    TEXT = "text"
    VOICE = "voice"
    PHOTO = "photo"
    VIDEO = "video"
    MIXED = "mixed"


class MediaKind(str, Enum):
    PHOTO = "photo"
    VIDEO = "video"


class MediaFlag(str, Enum):
    """Reasons a photo must not go out without a human looking at it."""

    BLURRY = "blurry"
    DUPLICATE = "duplicate"
    UNRELATED = "unrelated"
    INAPPROPRIATE = "inappropriate"
    PERSONAL_INFORMATION = "personal_information"
    UNSAFE_CONDUCT = "unsafe_conduct"
    SECURITY = "security"
    CONFIDENTIAL_DOCUMENT = "confidential_document"


#: Flags that block a photo from a client-facing message outright.
BLOCKING_MEDIA_FLAGS = {
    MediaFlag.INAPPROPRIATE,
    MediaFlag.PERSONAL_INFORMATION,
    MediaFlag.UNSAFE_CONDUCT,
    MediaFlag.SECURITY,
    MediaFlag.CONFIDENTIAL_DOCUMENT,
}


class Verification(str, Enum):
    """Whether the platform could corroborate what the field reported."""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    CONFLICTED = "conflicted"


class EscalationCategory(str, Enum):
    INJURY_OR_HAZARD = "injury_or_hazard"
    PROPERTY_DAMAGE = "property_damage"
    OUT_OF_SCOPE_WORK = "out_of_scope_work"
    POTENTIAL_CHANGE_ORDER = "potential_change_order"
    SCHEDULE_DELAY = "schedule_delay"
    CLIENT_DISSATISFACTION = "client_dissatisfaction"
    TRADE_DISPUTE = "trade_dispute"
    DEFECTIVE_WORK = "defective_work"
    MISSING_MATERIALS = "missing_materials"
    SITE_ACCESS = "site_access"
    EMPLOYEE_MISCONDUCT = "employee_misconduct"
    ABUSIVE_COMMUNICATION = "abusive_communication"
    LEGAL_OR_INSURANCE = "legal_or_insurance"
    MONEY_OR_CONTRACT = "money_or_contract"
    UNRESOLVED_CONTRADICTION = "unresolved_contradiction"


class Severity(str, Enum):
    INFO = "info"
    ATTENTION = "attention"
    URGENT = "urgent"
    CRITICAL = "critical"


class OpenItemKind(str, Enum):
    CLARIFICATION = "clarification"
    DECISION = "decision"
    APPROVAL = "approval"
    ACCESS = "access"
    INFORMATION = "information"
    REPAIR = "repair"


@dataclass
class Project:
    name: str
    number: str = ""
    address: str = ""
    client_name: str = ""
    client_account_id: str = ""           # links to the growth-side account
    general_contractor: str = ""
    scope_summary: str = ""
    exclusions: list[str] = field(default_factory=list)
    milestones: list[dict[str, Any]] = field(default_factory=list)
    status: ProjectStatus = ProjectStatus.ACTIVE
    site_access_notes: str = ""
    preferred_channel: CommChannel = CommChannel.EMAIL
    daily_log_time: str = ""              # "16:30" — when the client expects it
    report_length: str = "standard"       # brief | standard | detailed
    share_crew_count: bool = True         # some clients treat headcount as private
    share_work_period: bool = True
    notes: str = ""
    source: str = ""
    id: str = field(default_factory=lambda: new_id("proj"))
    created_at: str = field(default_factory=now_iso)

    @property
    def live(self) -> bool:
        return self.status in LIVE_STATUSES

    def to_dict(self) -> dict[str, Any]:
        data = serialize(self)
        data["live"] = self.live
        return data


@dataclass
class ProjectContact:
    """Someone who receives communication about a project."""

    project_id: str
    name: str
    role: RecipientRole = RecipientRole.CLIENT
    company: str = ""
    email: str = ""
    phone: str = ""
    preferred_channel: CommChannel = CommChannel.EMAIL
    receives_daily_log: bool = False
    notes: str = ""
    id: str = field(default_factory=lambda: new_id("pcon"))

    def address_for(self, channel: CommChannel | str) -> str:
        return self.phone if CommChannel(channel) is CommChannel.SMS else self.email

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass
class CrewMember:
    name: str
    role: str = "painter"                 # painter | crew_lead | foreman | apprentice
    phone: str = ""
    email: str = ""
    preferred_language: str = "en"        # the language they report in
    project_ids: list[str] = field(default_factory=list)
    notes: str = ""
    id: str = field(default_factory=lambda: new_id("crew"))

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass
class TimeRecord:
    """One clock-in/clock-out pair. The corroboration source for reported hours."""

    project_id: str
    crew_id: str
    work_date: str
    clock_in: str = ""
    clock_out: str = ""
    hours: float = 0.0
    crew_name: str = ""
    id: str = field(default_factory=lambda: new_id("time"))

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass
class FieldSubmission:
    """What a worker actually sent, plus everything derived from it.

    `original_text` and `original_language` are written once. Translation
    and cleanup land in separate fields so the chain from raw submission to
    sent message stays auditable.
    """

    project_id: str
    submitted_by: str = ""                # crew_... id
    submitted_by_name: str = ""
    kind: SubmissionKind = SubmissionKind.TEXT
    original_text: str = ""
    original_language: str = "unknown"
    transcript: str = ""                  # voice recordings, transcribed verbatim
    translation_en: str = ""
    normalized_summary: str = ""          # professional English, slang and blame removed
    work_areas: list[str] = field(default_factory=list)
    activities: list[str] = field(default_factory=list)
    hours_reported: float = 0.0
    crew_count_reported: int = 0
    media_ids: list[str] = field(default_factory=list)
    work_date: str = field(default_factory=today_iso)
    verification: Verification = Verification.UNVERIFIED
    conflicts: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    revisions: list[dict[str, Any]] = field(default_factory=list)
    submitted_at: str = field(default_factory=now_iso)
    id: str = field(default_factory=lambda: new_id("fsub"))

    @property
    def processed(self) -> bool:
        """True once there is usable English and a cleaned-up summary."""
        has_english = bool(self.translation_en.strip()) or self.original_language == "en"
        return has_english and bool(self.normalized_summary.strip())

    @property
    def source_text(self) -> str:
        """The best available rendering of what the worker said."""
        return self.transcript.strip() or self.original_text.strip()

    def to_dict(self) -> dict[str, Any]:
        data = serialize(self)
        data["processed"] = self.processed
        return data


@dataclass
class MediaAsset:
    project_id: str
    uri: str
    media_kind: MediaKind = MediaKind.PHOTO
    submission_id: str = ""
    caption: str = ""
    work_area: str = ""
    taken_on: str = field(default_factory=today_iso)
    flags: list[MediaFlag] = field(default_factory=list)
    client_safe: bool = False
    sequence: int = 0                     # display order within a report
    notes: str = ""
    id: str = field(default_factory=lambda: new_id("med"))

    @property
    def blocked(self) -> bool:
        return any(MediaFlag(f) in BLOCKING_MEDIA_FLAGS for f in self.flags)

    @property
    def needs_review(self) -> bool:
        return bool(self.flags)

    def to_dict(self) -> dict[str, Any]:
        data = serialize(self)
        data["blocked"] = self.blocked
        data["needs_review"] = self.needs_review
        return data


@dataclass
class DailyLog:
    """The client-facing daily record, in the sections the spec defines."""

    project_id: str
    log_date: str = field(default_factory=today_iso)
    crew_count: int = 0
    work_period: str = ""
    completed: list[str] = field(default_factory=list)
    in_progress: list[str] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    site_conditions: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    next_day: list[str] = field(default_factory=list)
    safety: list[str] = field(default_factory=list)
    photo_ids: list[str] = field(default_factory=list)
    source_submission_ids: list[str] = field(default_factory=list)
    #: Internal only. Never rendered into the client-facing body.
    unverified_notes: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    status: str = "draft"                 # draft | sent
    communication_id: str = ""
    created_at: str = field(default_factory=now_iso)
    id: str = field(default_factory=lambda: new_id("dlog"))

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass
class Communication:
    """One message, from draft through delivery to reply.

    This is the record required by the recordkeeping section: who it went
    to, on what channel, what it said, what was attached, whether it
    arrived, who approved it and what came back.
    """

    project_id: str
    kind: CommKind = CommKind.OTHER
    channel: CommChannel = CommChannel.EMAIL
    recipient_name: str = ""
    recipient_role: RecipientRole = RecipientRole.CLIENT
    recipient_address: str = ""
    contact_id: str = ""
    recipient_verified: bool = False      # matched to a stored project contact
    subject: str = ""
    body: str = ""
    attachments: list[str] = field(default_factory=list)   # med_... ids
    template_id: str = ""
    status: DeliveryStatus = DeliveryStatus.DRAFT
    risk_categories: list[str] = field(default_factory=list)
    style_warnings: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)    # daily log / submission ids
    requires_response: bool = False
    response_due: str = ""
    approval_id: str = ""
    approval_source: str = ""
    sent_at: str = ""
    delivery_detail: dict[str, Any] = field(default_factory=dict)
    responses: list[dict[str, Any]] = field(default_factory=list)
    corrects: str = ""                    # comm_... id this corrects
    channel_reason: str = ""
    created_at: str = field(default_factory=now_iso)
    id: str = field(default_factory=lambda: new_id("comm"))

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass
class OpenItem:
    """A question, decision or action waiting on somebody."""

    project_id: str
    question: str
    kind: OpenItemKind = OpenItemKind.CLARIFICATION
    asked_of: str = ""
    asked_of_role: RecipientRole = RecipientRole.CLIENT
    communication_id: str = ""
    due_date: str = ""
    status: str = "open"                  # open | answered | cancelled
    answer: str = ""
    answered_on: str = ""
    raised_on: str = field(default_factory=today_iso)
    id: str = field(default_factory=lambda: new_id("item"))

    def overdue(self, as_of: str = "") -> bool:
        if self.status != "open" or not self.due_date:
            return False
        return self.due_date < (as_of or today_iso())

    def to_dict(self) -> dict[str, Any]:
        data = serialize(self)
        data["overdue"] = self.overdue()
        return data


@dataclass
class Escalation:
    """A management notification, in the shape the spec requires.

    Every field below is mandatory in the escalation section: a factual
    summary, supporting evidence, known impact, what is still missing, a
    recommended next action and a proposed response for approval.
    """

    project_id: str
    category: EscalationCategory
    summary: str
    evidence: str = ""
    impact: str = ""
    missing_information: str = ""
    recommended_action: str = ""
    proposed_response: str = ""
    severity: Severity = Severity.ATTENTION
    status: str = "open"                  # open | acknowledged | closed
    notified_to: list[str] = field(default_factory=list)
    communication_id: str = ""
    source_ids: list[str] = field(default_factory=list)
    raised_on: str = field(default_factory=today_iso)
    created_at: str = field(default_factory=now_iso)
    id: str = field(default_factory=lambda: new_id("esc"))

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)
