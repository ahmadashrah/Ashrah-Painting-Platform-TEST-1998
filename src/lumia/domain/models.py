"""Core business objects for a painting company."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class JobType(str, Enum):
    INTERIOR = "interior"
    EXTERIOR = "exterior"
    CABINET = "cabinet"
    DECK_FENCE = "deck_fence"
    COMMERCIAL = "commercial"


class SurfaceCondition(str, Enum):
    """Drives how much prep labor a surface needs."""

    NEW = "new"           # new drywall / new construction
    GOOD = "good"         # sound paint, light clean and cut-in
    FAIR = "fair"         # some patching, minor scraping
    POOR = "poor"         # heavy scraping, peeling, wood repair


class LeadStatus(str, Enum):
    NEW = "new"
    QUALIFIED = "qualified"
    QUOTED = "quoted"
    WON = "won"
    LOST = "lost"
    UNQUALIFIED = "unqualified"


@dataclass
class Lead:
    name: str
    phone: str = ""
    email: str = ""
    address: str = ""
    job_type: JobType = JobType.INTERIOR
    notes: str = ""
    source: str = "website"
    status: LeadStatus = LeadStatus.NEW
    score: int = 0            # 0-100 qualification score
    id: str = field(default_factory=lambda: new_id("lead"))
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass
class Surface:
    """One paintable area within a job."""

    name: str                       # "living room walls", "north elevation siding"
    square_feet: float
    coats: int = 2
    condition: SurfaceCondition = SurfaceCondition.GOOD
    height_feet: float = 9.0        # over 10ft means staging/ladder work
    needs_primer: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass
class LineItem:
    description: str
    quantity: float
    unit: str
    unit_price: float

    @property
    def total(self) -> float:
        return round(self.quantity * self.unit_price, 2)

    def to_dict(self) -> dict[str, Any]:
        d = _serialize(self)
        d["total"] = self.total
        return d


@dataclass
class Quote:
    lead_id: str
    job_type: JobType
    line_items: list[LineItem] = field(default_factory=list)
    labor_hours: float = 0.0
    gallons_needed: float = 0.0
    subtotal: float = 0.0
    overhead: float = 0.0
    margin: float = 0.0
    total: float = 0.0
    valid_days: int = 30
    assumptions: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("quote"))
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass
class Crew:
    name: str
    size: int = 2
    skills: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("crew"))

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass
class ScheduledJob:
    quote_id: str
    crew_id: str
    start_date: date
    days: int
    job_type: JobType
    address: str = ""
    weather_risk: str = "unknown"
    id: str = field(default_factory=lambda: new_id("job"))

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


def _serialize(obj: Any) -> dict[str, Any]:
    """asdict() with enums and dates flattened to primitives."""

    def convert(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v) for v in value]
        return value

    return {k: convert(v) for k, v in asdict(obj).items()}
