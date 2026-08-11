"""Customer communication — transactional email and SMS."""

from __future__ import annotations

from typing import Any

from .base import Integration


class EmailService(Integration):
    """SendGrid-shaped transactional email."""

    def send(self, to: str, subject: str, body: str, from_email: str) -> dict[str, Any]:
        payload = {
            "personalizations": [{"to": [{"email": to}], "subject": subject}],
            "from": {"email": from_email},
            "content": [{"type": "text/plain", "value": body}],
        }
        return self.request(
            "POST",
            "/mail/send",
            json=payload,
            mock={"status": "queued", "to": to, "subject": subject, "preview": body[:400]},
        )


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
