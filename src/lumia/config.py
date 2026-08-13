"""Central configuration.

Every external service is optional. If its credentials are missing the
matching integration runs in mock mode instead of failing, so the whole
platform is runnable straight after `git clone`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# The model the agents run on. Opus 5 with adaptive thinking is the default;
# override with ASHRAH_MODEL if you want to trade capability for cost.
DEFAULT_MODEL = "claude-opus-5"

# Effort controls how much the model thinks and acts per turn.
# low | medium | high | xhigh | max
DEFAULT_EFFORT = "high"

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so we don't take a python-dotenv dependency."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        # Real environment variables always win over the file.
        os.environ.setdefault(key, value)


_load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class ServiceCredentials:
    """Credentials for one external API."""

    name: str
    api_key: str | None = None
    base_url: str | None = None
    extra: dict[str, str] = field(default_factory=dict)
    #: The environment variable this key comes from. Carried so `status` can
    #: say *which* variable to set rather than only that one is missing.
    key_var: str = ""
    also_needs: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def missing_vars(self) -> list[str]:
        """Which variables still have to be set for this service to go live."""
        if self.configured:
            return [v for v in self.also_needs if not os.environ.get(v)]
        return [self.key_var, *[v for v in self.also_needs if not os.environ.get(v)]]


def _service(name: str, key_var: str, url_var: str, default_url: str, **extra_vars: str) -> ServiceCredentials:
    return ServiceCredentials(
        name=name,
        api_key=os.environ.get(key_var) or None,
        base_url=os.environ.get(url_var) or default_url,
        extra={k: os.environ[v] for k, v in extra_vars.items() if os.environ.get(v)},
        key_var=key_var,
        also_needs=tuple(extra_vars.values()),
    )


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    model: str
    effort: str
    company_name: str
    company_phone: str
    company_email: str
    data_dir: Path
    services: dict[str, ServiceCredentials]

    def service(self, name: str) -> ServiceCredentials:
        return self.services.get(name, ServiceCredentials(name=name))


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("ASHRAH_DATA_DIR", PROJECT_ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    services = {
        # Record of truth for accounts, contacts and pipeline.
        "crm": _service("crm", "CRM_API_KEY", "CRM_BASE_URL", "https://api.example-crm.com/v1"),
        # Outreach email.
        "email": _service("email", "SENDGRID_API_KEY", "SENDGRID_BASE_URL", "https://api.sendgrid.com/v3"),
        # SMS — used sparingly in B2B, mostly for site coordination.
        "sms": _service(
            "sms",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_BASE_URL",
            "https://api.twilio.com/2010-04-01",
            account_sid="TWILIO_ACCOUNT_SID",
            from_number="TWILIO_FROM_NUMBER",
        ),
        # Meetings and site walks.
        "calendar": _service(
            "calendar",
            "GOOGLE_CALENDAR_TOKEN",
            "GOOGLE_CALENDAR_BASE_URL",
            "https://www.googleapis.com/calendar/v3",
            calendar_id="GOOGLE_CALENDAR_ID",
        ),
        # Market research.
        "search": _service("search", "SEARCH_API_KEY", "SEARCH_BASE_URL", "https://api.search.brave.com/res/v1/web"),
        # Permits, tenders, project awards.
        "construction_data": _service(
            "construction_data",
            "CONSTRUCTION_DATA_API_KEY",
            "CONSTRUCTION_DATA_BASE_URL",
            "https://api.example-construction-data.com/v1",
        ),
        # Weather — exterior scope can't be promised into a wet week.
        "weather": _service(
            "weather",
            "OPENWEATHER_API_KEY",
            "OPENWEATHER_BASE_URL",
            "https://api.openweathermap.org/data/2.5",
        ),
    }

    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        model=os.environ.get("ASHRAH_MODEL", DEFAULT_MODEL),
        effort=os.environ.get("ASHRAH_EFFORT", DEFAULT_EFFORT),
        company_name=os.environ.get("COMPANY_NAME", "Ashrah Painting Ltd."),
        company_phone=os.environ.get("COMPANY_PHONE", ""),
        company_email=os.environ.get("COMPANY_EMAIL", ""),
        data_dir=data_dir,
        services=services,
    )


SETTINGS = load_settings()
