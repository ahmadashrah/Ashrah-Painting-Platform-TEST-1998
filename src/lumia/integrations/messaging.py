"""Customer communication — transactional email and SMS."""

from __future__ import annotations

from typing import Any

from .base import Integration


class EmailService(Integration):
    """Transactional email, over Resend or SendGrid.

    Which provider a deployment can get a key for is not this code's
    decision, so both are supported. They differ in more than a hostname —
    Resend takes a flat body at `/emails`, SendGrid a nested one at
    `/mail/send` — and the branch is kept visible here rather than hidden
    behind a shared schema that would fit neither.

    Neither returns the same success shape either: Resend answers with a
    message id, SendGrid with an empty 202. Both are passed back as they
    came. `mark_sent` treats a delivery as failed only on an explicit error,
    so a provider that says little is not mistaken for one that failed.
    """

    @property
    def provider(self) -> str:
        return self.credentials.extra.get("provider") or "sendgrid"

    def send(self, to: str, subject: str, body: str, from_email: str) -> dict[str, Any]:
        mock = {"status": "queued", "to": to, "subject": subject, "preview": body[:400]}

        if self.provider == "resend":
            return self.request(
                "POST",
                "/emails",
                json={"from": from_email, "to": [to], "subject": subject, "text": body},
                mock=mock,
            )

        payload = {
            "personalizations": [{"to": [{"email": to}], "subject": subject}],
            "from": {"email": from_email},
            "content": [{"type": "text/plain", "value": body}],
        }
        return self.request("POST", "/mail/send", json=payload, mock=mock)


class SMSService(Integration):
    """Twilio-shaped SMS."""

    def _headers(self) -> dict[str, str]:
        # Twilio uses basic auth (account SID + auth token), not bearer.
        import base64

        sid = self.credentials.extra.get("account_sid", "")
        token = self.credentials.api_key or ""
        encoded = base64.b64encode(f"{sid}:{token}".encode()).decode()
        return {
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    @property
    def live(self) -> bool:
        # Twilio needs both halves of the credential plus a sending number.
        return bool(
            self.credentials.api_key
            and self.credentials.extra.get("account_sid")
            and self.credentials.extra.get("from_number")
        )

    def send(self, to: str, body: str) -> dict[str, Any]:
        if len(body) > 1600:
            body = body[:1597] + "..."
        sid = self.credentials.extra.get("account_sid", "")
        payload = {
            "To": to,
            "From": self.credentials.extra.get("from_number", ""),
            "Body": body,
        }
        return self.request(
            "POST",
            f"/Accounts/{sid}/Messages.json",
            json=payload,
            mock={"status": "queued", "to": to, "preview": body},
        )
