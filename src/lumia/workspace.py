"""Wires configuration into a live set of services.

One Workspace is constructed per process and handed to the toolbox and the
agents. Everything stateful lives here so tests can build a Workspace on a
temp directory and get full isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .autonomy import ApprovalQueue
from .config import SETTINGS, Settings
from .integrations.crm import CRM
from .integrations.messaging import EmailService, SMSService
from .integrations.operations import OperationsSystem, Timeclock
from .integrations.research import ConstructionData, WebSearch
from .integrations.scheduling import CalendarService, WeatherService
from .memory import Memory
from .store import LocalStore


@dataclass
class Workspace:
    settings: Settings
    store: LocalStore
    crm: CRM
    memory: Memory
    approvals: ApprovalQueue
    email: EmailService
    sms: SMSService
    calendar: CalendarService
    weather: WeatherService
    search: WebSearch
    construction: ConstructionData
    ops: OperationsSystem
    timeclock: Timeclock

    @classmethod
    def build(cls, settings: Settings | None = None, data_dir: Path | None = None) -> "Workspace":
        settings = settings or SETTINGS
        root = data_dir or settings.data_dir
        root.mkdir(parents=True, exist_ok=True)

        store = LocalStore(root / "lumia.json")
        return cls(
            settings=settings,
            store=store,
            crm=CRM(settings.service("crm"), store),
            memory=Memory(store),
            approvals=ApprovalQueue(root / "approvals.json"),
            email=EmailService(settings.service("email")),
            sms=SMSService(settings.service("sms")),
            calendar=CalendarService(settings.service("calendar")),
            weather=WeatherService(settings.service("weather")),
            search=WebSearch(settings.service("search")),
            construction=ConstructionData(settings.service("construction_data")),
            ops=OperationsSystem(settings.service("operations"), store),
            timeclock=Timeclock(settings.service("timeclock")),
        )

    def status(self) -> dict[str, object]:
        """Which integrations are live vs simulated — shown at startup."""
        services = {
            "crm": self.crm,
            "email": self.email,
            "sms": self.sms,
            "calendar": self.calendar,
            "weather": self.weather,
            "search": self.search,
            "construction_data": self.construction,
            "operations": self.ops,
            "timeclock": self.timeclock,
        }
        return {
            "model": self.settings.model,
            "effort": self.settings.effort,
            "anthropic_key_set": bool(self.settings.anthropic_api_key),
            "data_dir": str(self.settings.data_dir),
            "integrations": {name: ("live" if svc.live else "mock") for name, svc in services.items()},
            "pending_approvals": len(self.approvals.pending()),
        }
