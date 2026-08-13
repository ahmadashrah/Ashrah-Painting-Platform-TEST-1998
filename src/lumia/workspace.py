"""Wires configuration into a live set of services.

One Workspace is constructed per process and handed to the toolbox and the
agents. Everything stateful lives here so tests can build a Workspace on a
temp directory and get full isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .autonomy import ApprovalQueue
from .comms.ledger import CommunicationLedger
from .config import SETTINGS, Settings
from .integrations.crm import CRM
from .integrations.messaging import EmailService, SMSService
from .integrations.openai import OpenAIService
from .integrations.research import ConstructionData, WebSearch
from .integrations.scheduling import CalendarService, WeatherService
from .memory import Memory
from .store import LocalStore


@dataclass
class Workspace:
    settings: Settings
    store: LocalStore
    crm: CRM
    comms: CommunicationLedger
    memory: Memory
    approvals: ApprovalQueue
    email: EmailService
    sms: SMSService
    calendar: CalendarService
    weather: WeatherService
    search: WebSearch
    construction: ConstructionData
    openai: OpenAIService
    #: The run this workspace belongs to, once one has been issued a number.
    #: A workspace is built per run, so this is the run's scope.
    run_ref: str = ""

    def stamp_run(self, reference: str) -> None:
        """Bind this workspace to a run, so every write records which one."""
        self.run_ref = reference
        self.store.run_ref = reference

    @classmethod
    def build(cls, settings: Settings | None = None, data_dir: Path | None = None) -> "Workspace":
        settings = settings or SETTINGS
        root = data_dir or settings.data_dir
        root.mkdir(parents=True, exist_ok=True)

        store = LocalStore(root / "lumia.json")
        openai = OpenAIService(settings.service("openai"))
        return cls(
            settings=settings,
            store=store,
            crm=CRM(settings.service("crm"), store),
            comms=CommunicationLedger(store),
            # Memory embeds through OpenAI when configured, and falls back to
            # keyword matching when it is not — see Memory.recall.
            memory=Memory(store, embedder=openai, model=settings.embed_model),
            openai=openai,
            approvals=ApprovalQueue(root / "approvals.json"),
            email=EmailService(settings.service("email")),
            sms=SMSService(settings.service("sms")),
            calendar=CalendarService(settings.service("calendar")),
            weather=WeatherService(settings.service("weather")),
            search=WebSearch(settings.service("search")),
            construction=ConstructionData(settings.service("construction_data")),
        )

    def status(self) -> dict[str, object]:
        """Which integrations are live vs simulated — shown at startup."""
        services = {
            "crm": self.crm,
            "openai": self.openai,
            "email": self.email,
            "sms": self.sms,
            "calendar": self.calendar,
            "weather": self.weather,
            "search": self.search,
            "construction_data": self.construction,
        }
        return {
            "model": self.settings.model,
            "effort": self.settings.effort,
            "anthropic_key_set": bool(self.settings.anthropic_api_key),
            "data_dir": str(self.settings.data_dir),
            "integrations": {name: ("live" if svc.live else "mock") for name, svc in services.items()},
            "pending_approvals": len(self.approvals.pending()),
            "active_projects": len(self.comms.active_projects()),
            "open_escalations": len(self.comms.escalations(status="open")),
        }
