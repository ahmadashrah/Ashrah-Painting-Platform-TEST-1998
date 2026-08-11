"""Choosing the channel.

The spec gives the rule plainly: email for formal reports, attachments,
schedule changes and anything needing a clear record; text for short
reminders, arrival notices, access coordination, urgent questions and
confirmations. Recipient preference matters, but it does not override a
message that needs a record or carries attachments.

This is deterministic on purpose. Channel choice affects whether a
decision is recoverable six months later, which is not something to
re-derive by feel on every message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.projects import CommChannel, CommKind

#: Beyond this, a text message fragments and stops being a text message.
SMS_LIMIT = 320

#: Kinds that are formal records regardless of length.
RECORD_KINDS = {
    CommKind.DAILY_LOG,
    CommKind.CLIENT_UPDATE,
    CommKind.COMMITMENT,
    CommKind.VENDOR_REQUEST,
    CommKind.ESCALATION_NOTICE,
}

#: Kinds that are short by nature and suit a text message.
SHORT_KINDS = {
    CommKind.ARRIVAL_NOTICE,
    CommKind.SCHEDULE_REMINDER,
    CommKind.CONFIRMATION_REQUEST,
    CommKind.CLARIFICATION_REQUEST,
}


@dataclass
class ChannelChoice:
    channel: CommChannel
    reason: str
    overrode_preference: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel.value,
            "reason": self.reason,
            "overrode_preference": self.overrode_preference,
            # The spec: a decision made by text is still saved to the project
            # record. That is structural here — the ledger stores every
            # message on either channel — so it is stated, not optional.
            "recorded_in_project": True,
        }


def _as_kind(kind: CommKind | str) -> CommKind:
    try:
        return CommKind(kind)
    except ValueError:
        return CommKind.OTHER


def _as_channel(value: CommChannel | str | None) -> CommChannel | None:
    if not value:
        return None
    try:
        return CommChannel(value)
    except ValueError:
        return None


def choose_channel(
    *,
    kind: CommKind | str = CommKind.OTHER,
    recipient_preference: CommChannel | str = "",
    urgency: str = "normal",
    body_length: int = 0,
    has_attachments: bool = False,
    needs_record: bool = False,
) -> ChannelChoice:
    """Pick a channel and say which rule decided it."""
    message_kind = _as_kind(kind)
    preference = _as_channel(recipient_preference)
    urgent = str(urgency).lower() in {"urgent", "high"}

    if has_attachments:
        return ChannelChoice(
            CommChannel.EMAIL,
            "Email: the message carries attachments.",
            overrode_preference=preference is CommChannel.SMS,
        )

    if message_kind in RECORD_KINDS or needs_record:
        return ChannelChoice(
            CommChannel.EMAIL,
            f"Email: a {message_kind.value.replace('_', ' ')} is a formal record.",
            overrode_preference=preference is CommChannel.SMS,
        )

    if body_length > SMS_LIMIT:
        return ChannelChoice(
            CommChannel.EMAIL,
            f"Email: the message is {body_length} characters, past the {SMS_LIMIT}-character "
            "point where a text stops being readable.",
            overrode_preference=preference is CommChannel.SMS,
        )

    if urgent:
        return ChannelChoice(
            CommChannel.SMS,
            "Text: the message is urgent and short enough to read on a phone.",
            overrode_preference=preference is CommChannel.EMAIL,
        )

    if preference is not None:
        return ChannelChoice(preference, f"{preference.value.upper()}: the recipient's stated preference.")

    if message_kind in SHORT_KINDS:
        return ChannelChoice(
            CommChannel.SMS,
            f"Text: a {message_kind.value.replace('_', ' ')} is a short, time-sensitive note.",
        )

    return ChannelChoice(CommChannel.EMAIL, "Email: the default where nothing argues for a text.")
