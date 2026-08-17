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

# OpenAI models, used where they are the better tool rather than as a
# wholesale replacement. Whisper handles the field's Arabic, Kurdish and
# French voice notes; the vision model reads submitted photos; embeddings
# give memory recall by meaning instead of by shared keywords.
DEFAULT_TRANSCRIBE_MODEL = "whisper-1"
DEFAULT_VISION_MODEL = "gpt-4o-mini"
DEFAULT_EMBED_MODEL = "text-embedding-3-small"


def provider_for(model: str) -> str:
    """Which API a model name belongs to.

    Inferred rather than configured separately, so setting ASHRAH_MODEL to a
    GPT model is all it takes to move an agent across — there is no second
    switch to forget.
    """
    name = model.lower()
    if name.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    return "anthropic"

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: True when running from a source checkout rather than an installed package.
IN_CHECKOUT = (PROJECT_ROOT / "src" / "lumia").is_dir()


def default_data_dir() -> Path:
    """Where agent state lives when ASHRAH_DATA_DIR is not set.

    In a checkout that is `./data` beside the source. Installed, the same
    expression resolves inside site-packages — which is the wrong place on
    every count: it is ephemeral on a container host, often read-only, and
    shared between every project using that interpreter. Fall back to the
    working directory, which on a deployment is the app root.
    """
    return (PROJECT_ROOT if IN_CHECKOUT else Path.cwd()) / "data"


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


def _email_service() -> ServiceCredentials:
    """Email, from whichever provider has a key.

    Resend and SendGrid are not one service behind two hostnames. They take
    different keys, and they take different payload shapes, so which one is
    in play has to be decided once — here — and carried with the credentials
    rather than guessed at the call site.

    Resend wins when both are set. A key someone has just pasted in should
    not lose to one left over from an earlier attempt, and a deployment
    quietly sending through the provider you thought you had replaced is
    worse than one that refuses.
    """
    if os.environ.get("RESEND_API_KEY"):
        return ServiceCredentials(
            name="email",
            api_key=os.environ["RESEND_API_KEY"],
            base_url=os.environ.get("RESEND_BASE_URL") or "https://api.resend.com",
            extra={"provider": "resend"},
            key_var="RESEND_API_KEY",
        )
    if os.environ.get("SENDGRID_API_KEY"):
        return ServiceCredentials(
            name="email",
            api_key=os.environ["SENDGRID_API_KEY"],
            base_url=os.environ.get("SENDGRID_BASE_URL") or "https://api.sendgrid.com/v3",
            extra={"provider": "sendgrid"},
            key_var="SENDGRID_API_KEY",
        )
    # Neither is set. `status` names exactly one variable to add, so it names
    # the shorter path to a working sender.
    return ServiceCredentials(
        name="email",
        base_url="https://api.resend.com",
        extra={"provider": "resend"},
        key_var="RESEND_API_KEY",
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
    transcribe_model: str = DEFAULT_TRANSCRIBE_MODEL
    vision_model: str = DEFAULT_VISION_MODEL
    embed_model: str = DEFAULT_EMBED_MODEL

    def service(self, name: str) -> ServiceCredentials:
        return self.services.get(name, ServiceCredentials(name=name))

    @property
    def provider(self) -> str:
        """Which API the agents' model belongs to."""
        return provider_for(self.model)


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("ASHRAH_DATA_DIR") or default_data_dir())
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Importing the package must not explode because a directory could
        # not be made. Fall back to a temporary one and say so loudly — a
        # deployment that cannot persist should still boot and report.
        import tempfile

        fallback = Path(tempfile.gettempdir()) / "lumia-data"
        fallback.mkdir(parents=True, exist_ok=True)
        print(
            f"warning: cannot write to {data_dir} ({exc}); using {fallback}. "
            "Set ASHRAH_DATA_DIR to a writable path — on Railway, attach a volume, "
            "or state will be lost on every redeploy.",
            flush=True,
        )
        data_dir = fallback

    services = {
        # OpenAI: speech-to-text for field voice notes, vision for submitted
        # photos, embeddings for memory recall, and GPT models as a second
        # brain for the agent loop.
        "openai": _service("openai", "OPENAI_API_KEY", "OPENAI_BASE_URL", "https://api.openai.com/v1"),
        # Record of truth for accounts, contacts and pipeline.
        "crm": _service("crm", "CRM_API_KEY", "CRM_BASE_URL", "https://api.example-crm.com/v1"),
        # Outreach and project email, over Resend or SendGrid.
        "email": _email_service(),
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
        transcribe_model=os.environ.get("OPENAI_TRANSCRIBE_MODEL", DEFAULT_TRANSCRIBE_MODEL),
        vision_model=os.environ.get("OPENAI_VISION_MODEL", DEFAULT_VISION_MODEL),
        embed_model=os.environ.get("OPENAI_EMBED_MODEL", DEFAULT_EMBED_MODEL),
    )


SETTINGS = load_settings()
