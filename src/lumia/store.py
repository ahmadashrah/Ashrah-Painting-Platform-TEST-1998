"""A small JSON-file store.

Stands in for a database so the whole system runs after `git clone` with no
infrastructure. Swap this class for a real backend and nothing above it has
to change — every caller goes through `put` / `get` / `list` / `search`.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

#: Collections the platform expects to exist.
COLLECTIONS = (
    # Growth side: accounts, pipeline and what was learned from working it.
    "accounts",
    "contacts",
    "interactions",
    "opportunities",
    "signals",
    "are_records",
    "lessons",
    "proposals",
    "content",
    "quotes",
    # Delivery side: projects, what the field reported and what was sent out.
    "projects",
    "project_contacts",
    "crew",
    "time_records",
    "submissions",
    "media",
    "daily_logs",
    "communications",
    "open_items",
    "escalations",
    # Every agent run, isolated and recorded — see runner.py.
    "runs",
)


#: Stamped onto every record written during a numbered run.
RUN_FIELD = "_run"


class LocalStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        #: The run currently writing through this store, if any. Set once per
        #: run (see Runner) rather than passed to every call — every write
        #: below then carries the run that caused it, including writes from
        #: tools that do not exist yet.
        self.run_ref = ""
        if not self.path.exists():
            self._write({name: {} for name in COLLECTIONS})

    def _stamp(self, value: dict[str, Any]) -> dict[str, Any]:
        if not self.run_ref:
            return value
        return {**value, RUN_FIELD: self.run_ref}

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for name in COLLECTIONS:
            data.setdefault(name, {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)

    def put(self, collection: str, key: str, value: dict[str, Any]) -> dict[str, Any]:
        stamped = self._stamp(value)
        with self._lock:
            data = self._read()
            data.setdefault(collection, {})[key] = stamped
            self._write(data)
        return stamped

    def patch(self, collection: str, key: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            data = self._read()
            record = data.setdefault(collection, {}).get(key)
            if record is None:
                return None
            record.update(self._stamp(changes))
            self._write(data)
            return record

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        return self._read().get(collection, {}).get(key)

    def list(self, collection: str) -> list[dict[str, Any]]:
        return list(self._read().get(collection, {}).values())

    def where(self, collection: str, **equals: Any) -> list[dict[str, Any]]:
        return [
            record
            for record in self.list(collection)
            if all(record.get(field) == value for field, value in equals.items())
        ]

    def search(self, collection: str, term: str) -> list[dict[str, Any]]:
        term = term.strip().lower()
        if not term:
            return []
        matches = []
        for record in self.list(collection):
            haystack = json.dumps(record, default=str).lower()
            if term in haystack:
                matches.append(record)
        return matches

    def delete(self, collection: str, key: str) -> bool:
        with self._lock:
            data = self._read()
            if key in data.get(collection, {}):
                del data[collection][key]
                self._write(data)
                return True
        return False
