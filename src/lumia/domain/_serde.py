"""One serializer for every domain dataclass.

Records are stored as plain JSON, so enums, dates and datetimes have to be
flattened on the way out. Keeping this in one place means a new domain
module cannot quietly grow a different idea of what a stored record looks
like.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from enum import Enum
from typing import Any


def convert(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: convert(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [convert(v) for v in value]
    return value


def serialize(obj: Any) -> dict[str, Any]:
    """asdict() with enums and dates flattened to primitives."""
    return {k: convert(v) for k, v in asdict(obj).items()}
