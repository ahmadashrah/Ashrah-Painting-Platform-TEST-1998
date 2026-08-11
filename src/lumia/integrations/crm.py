"""CRM integration — the record of truth for accounts, people and pipeline.

Writes go to the local store first (so read-after-write always works), then
mirror to a hosted CRM when one is configured. The mirror is best-effort:
a CRM outage must not lose the agent's work.
"""

from __future__ import annotations

import logging
from typing import Any

from ..domain.accounts import PipelineStage, TERMINAL_STAGES, today_iso
from ..store import LocalStore
from .base import Integration, IntegrationError

log = logging.getLogger(__name__)

#: Computed on read, never stored — see `_with_derived_account_fields`.
DERIVED_ACCOUNT_FIELDS = {"unmanaged"}


class CRM(Integration):
    def __init__(self, credentials, store: LocalStore) -> None:  # type: ignore[no-untyped-def]
        super().__init__(credentials=credentials)
        self.store = store

    # --- mirroring -----------------------------------------------------

    def _mirror(self, method: str, path: str, payload: dict[str, Any]) -> None:
        """Push a local write to the hosted CRM; never fail the caller."""
        if not self.live:
            return
        try:
            self.request(method, path, json=payload, mock={})
        except IntegrationError as exc:
            log.warning("CRM mirror to %s failed, local store is still authoritative: %s", path, exc)

    # --- accounts ------------------------------------------------------

    def upsert_account(self, account: dict[str, Any]) -> dict[str, Any]:
        # Derived fields are recomputed on read; never let one round-trip into storage.
        incoming = {k: v for k, v in account.items() if k not in DERIVED_ACCOUNT_FIELDS}
        existing = self.store.get("accounts", incoming["id"])
        if existing:
            # Blank values mean "not supplied" on a partial update, so they don't
            # overwrite. Use the dedicated setters to clear a field deliberately.
            existing.update({k: v for k, v in incoming.items() if v not in (None, "", [], {})})
            record = existing
        else:
            record = incoming
        self.store.put("accounts", record["id"], record)
        self._mirror("POST", "/accounts", record)
        return _with_derived_account_fields(record)

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        record = self.store.get("accounts", account_id)
        return _with_derived_account_fields(record) if record else None

    def find_accounts(self, term: str) -> list[dict[str, Any]]:
        return [_with_derived_account_fields(r) for r in self.store.search("accounts", term)]

    def list_accounts(self, **filters: Any) -> list[dict[str, Any]]:
        records = self.store.where("accounts", **filters) if filters else self.store.list("accounts")
        return [_with_derived_account_fields(r) for r in records]

    def set_next_action(self, account_id: str, action: str, due: str) -> dict[str, Any]:
        record = self.store.patch("accounts", account_id, {"next_action": action, "next_action_date": due})
        if record is None:
            return {"error": f"no account with id {account_id}"}
        self._mirror("PATCH", f"/accounts/{account_id}", {"next_action": action, "next_action_date": due})
        return record

    def advance_stage(self, account_id: str, stage: PipelineStage) -> dict[str, Any]:
        record = self.store.patch("accounts", account_id, {"stage": stage.value})
        if record is None:
            return {"error": f"no account with id {account_id}"}
        self._mirror("PATCH", f"/accounts/{account_id}", {"stage": stage.value})
        return record

    # --- contacts ------------------------------------------------------

    def upsert_contact(self, contact: dict[str, Any]) -> dict[str, Any]:
        self.store.put("contacts", contact["id"], contact)
        self._mirror("POST", "/contacts", contact)
        return contact

    def contacts_for(self, account_id: str) -> list[dict[str, Any]]:
        return self.store.where("contacts", account_id=account_id)

    # --- interactions --------------------------------------------------

    def log_interaction(self, interaction: dict[str, Any]) -> dict[str, Any]:
        self.store.put("interactions", interaction["id"], interaction)
        self.store.patch(
            "accounts",
            interaction["account_id"],
            {"last_interaction_date": interaction.get("occurred_on", today_iso())},
        )
        self._mirror("POST", "/interactions", interaction)
        return interaction

    def interactions_for(self, account_id: str) -> list[dict[str, Any]]:
        records = self.store.where("interactions", account_id=account_id)
        return sorted(records, key=lambda r: r.get("occurred_on", ""), reverse=True)

    # --- opportunities -------------------------------------------------

    def upsert_opportunity(self, opportunity: dict[str, Any]) -> dict[str, Any]:
        self.store.put("opportunities", opportunity["id"], opportunity)
        self._mirror("POST", "/opportunities", opportunity)
        return opportunity

    def opportunities_for(self, account_id: str) -> list[dict[str, Any]]:
        return [_with_weighted_value(o) for o in self.store.where("opportunities", account_id=account_id)]

    def open_opportunities(self) -> list[dict[str, Any]]:
        closed = {PipelineStage.WON.value, PipelineStage.LOST.value}
        return [
            _with_weighted_value(o)
            for o in self.store.list("opportunities")
            if o.get("stage") not in closed
        ]

    # --- signals -------------------------------------------------------

    def record_signal(self, signal: dict[str, Any]) -> dict[str, Any]:
        self.store.put("signals", signal["id"], signal)
        return signal

    def open_signals_for(self, account_name: str) -> list[dict[str, Any]]:
        name = account_name.strip().lower()
        if not name:
            return []
        return [s for s in self.store.list("signals") if name in str(s.get("likely_account", "")).lower()]

    # --- rollups -------------------------------------------------------

    def pipeline(self) -> dict[str, Any]:
        """Stage counts, pipeline value, and the accounts nobody is working."""
        accounts = self.store.list("accounts")
        opportunities = self.store.list("opportunities")
        terminal = {s.value for s in TERMINAL_STAGES}

        by_stage: dict[str, int] = {}
        for account in accounts:
            stage = str(account.get("stage", "unknown"))
            by_stage[stage] = by_stage.get(stage, 0) + 1

        open_opps = [o for o in opportunities if o.get("stage") not in {"won", "lost"}]
        pipeline_value = sum(float(o.get("estimated_value", 0) or 0) for o in open_opps)
        weighted = sum(
            float(o.get("estimated_value", 0) or 0) * float(o.get("probability", 0) or 0)
            for o in open_opps
        )

        unmanaged = [
            {"id": a["id"], "name": a.get("name", ""), "stage": a.get("stage", "")}
            for a in accounts
            if a.get("stage") not in terminal and not str(a.get("next_action", "")).strip()
        ]

        overdue = [
            {"id": a["id"], "name": a.get("name", ""), "due": a.get("next_action_date", ""), "action": a.get("next_action", "")}
            for a in accounts
            if a.get("next_action_date") and str(a["next_action_date"]) < today_iso()
        ]

        return {
            "total_accounts": len(accounts),
            "by_stage": by_stage,
            "by_tier": _count(accounts, "tier"),
            "open_opportunities": len(open_opps),
            "pipeline_value": round(pipeline_value, 2),
            "weighted_pipeline_value": round(weighted, 2),
            "unmanaged_accounts": unmanaged,
            "overdue_actions": overdue,
        }


def _with_derived_account_fields(record: dict[str, Any]) -> dict[str, Any]:
    """Recompute `unmanaged` on every read so it can never go stale."""
    enriched = dict(record)
    terminal = {s.value for s in TERMINAL_STAGES}
    enriched["unmanaged"] = (
        record.get("stage") not in terminal and not str(record.get("next_action", "")).strip()
    )
    return enriched


def _with_weighted_value(record: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(record)
    enriched["weighted_value"] = round(
        float(record.get("estimated_value", 0) or 0) * float(record.get("probability", 0) or 0), 2
    )
    return enriched


def _count(records: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get(field, "unknown"))
        counts[key] = counts.get(key, 0) + 1
    return counts
