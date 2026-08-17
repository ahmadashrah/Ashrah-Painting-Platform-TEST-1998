"""Email goes out over Resend or SendGrid, and the two are not interchangeable.

A key pasted into the host's variables has to actually pick the provider it
belongs to. The failure this guards against is silent: a `RESEND_API_KEY`
that nothing reads leaves email in mock mode, and mock mode reports
`"status": "queued"` — indistinguishable from a send, unless you check.
"""

from __future__ import annotations

from dataclasses import replace

from lumia.cli import _test_email
from lumia.config import ServiceCredentials, _email_service
from lumia.contract import EgressGuard
from lumia.integrations.messaging import EmailService


# --- which provider is chosen --------------------------------------------


def test_resend_key_selects_resend(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)

    credentials = _email_service()
    assert credentials.extra["provider"] == "resend"
    assert credentials.base_url == "https://api.resend.com"
    assert credentials.key_var == "RESEND_API_KEY"
    assert credentials.configured


def test_sendgrid_key_still_works(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.test")

    credentials = _email_service()
    assert credentials.extra["provider"] == "sendgrid"
    assert credentials.base_url == "https://api.sendgrid.com/v3"
    assert credentials.key_var == "SENDGRID_API_KEY"


def test_resend_wins_when_both_are_set(monkeypatch):
    """A key added deliberately should not lose to one left over."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.test")

    assert _email_service().extra["provider"] == "resend"


def test_neither_key_names_one_variable_to_add(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)

    credentials = _email_service()
    assert not credentials.configured
    assert credentials.missing_vars == ["RESEND_API_KEY"]


# --- what each provider is actually sent ---------------------------------


def _sent_call(provider: str):
    """Send one message in mock mode and return the call it recorded."""
    service = EmailService(
        ServiceCredentials(name="email", base_url="https://example.invalid", extra={"provider": provider})
    )
    service.send(
        to="owner@example.com",
        subject="Verification",
        body="This is a test.",
        from_email="growth@example.com",
    )
    return service.calls[-1]


def test_resend_gets_a_flat_body_at_emails():
    call = _sent_call("resend")
    assert call.path == "/emails"
    assert call.payload == {
        "from": "growth@example.com",
        "to": ["owner@example.com"],
        "subject": "Verification",
        "text": "This is a test.",
    }


def test_sendgrid_gets_its_nested_body_at_mail_send():
    call = _sent_call("sendgrid")
    assert call.path == "/mail/send"
    assert call.payload["personalizations"] == [
        {"to": [{"email": "owner@example.com"}], "subject": "Verification"}
    ]
    assert call.payload["from"] == {"email": "growth@example.com"}


def test_provider_defaults_to_sendgrid_when_unstated():
    """Credentials built before this split carried no provider."""
    service = EmailService(ServiceCredentials(name="email"))
    assert service.provider == "sendgrid"


# --- the egress allowlist follows the provider ---------------------------


def test_resend_host_is_allowed_once_it_is_the_configured_provider(monkeypatch):
    """The guard builds its allowlist from configured base URLs, so a new
    provider must not need a second place to register it."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test")

    class _Settings:
        services = {"email": _email_service()}

    guard = EgressGuard.from_settings(_Settings())
    assert guard.permitted("https://api.resend.com/emails")
    assert not guard.permitted("https://api.resend.com.evil.test/emails")


# --- the check that tells you the truth ----------------------------------


def test_test_email_refuses_rather_than_mocking(ws, capsys):
    """An unconfigured provider mocks, and a mock says "queued". Anyone
    checking a new key would read that as a send."""
    assert _test_email(ws, "owner@example.com") == 1

    out = capsys.readouterr().out
    assert "nothing would be sent" in out
    assert "RESEND_API_KEY" in out  # the variable, not just the problem


def test_test_email_refuses_without_a_sending_address(ws):
    ws.settings = replace(ws.settings, company_email="")
    assert _test_email(ws, "owner@example.com") == 1
