"""The tool surface Lumia acts through.

Every capability the agent has is a function here. Nothing reaches an
external service except through this module, which is what makes the
autonomy gate in `autonomy.py` enforceable rather than advisory.

Tool descriptions are prescriptive about *when* to call — that materially
improves how reliably the model reaches for the right one.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .domain.accounts import (
    Account,
    AccountTier,
    AccountType,
    ARERecord,
    Channel,
    Confidence,
    Contact,
    Interaction,
    MarketSignal,
    Opportunity,
    PipelineStage,
    new_id,
    today_iso,
)
from .domain.models import JobType, Surface, SurfaceCondition
from .domain.pricing import build_quote, estimate_duration_days
from .domain.scoring import prioritize, score_account
from .memory import ImprovementProposal, Lesson
from .workspace import Workspace


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    fn: Callable[..., Any]

    def anthropic_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.schema,
        }


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def string(description: str, enum: list[str] | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": "string", "description": description}
    if enum:
        spec["enum"] = enum
    return spec


def number(description: str) -> dict[str, Any]:
    return {"type": "number", "description": description}


def integer(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description}


def boolean(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def array(description: str, item_type: str = "string") -> dict[str, Any]:
    return {"type": "array", "description": description, "items": {"type": item_type}}


class Toolbox:
    """Binds tool functions to a live workspace and exposes their schemas."""

    def __init__(self, workspace: Workspace) -> None:
        self.ws = workspace
        self._tools: dict[str, ToolSpec] = {}
        self._register_all()

    # --- registry ------------------------------------------------------

    def register(self, name: str, description: str, schema: dict[str, Any], fn: Callable[..., Any]) -> None:
        self._tools[name] = ToolSpec(name=name, description=description, schema=schema, fn=fn)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, only: list[str] | None = None) -> list[dict[str, Any]]:
        selected = only or self.names()
        return [self._tools[n].anthropic_schema() for n in selected if n in self._tools]

    def has(self, name: str) -> bool:
        return name in self._tools

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            return {"error": f"unknown tool '{name}'"}
        signature = inspect.signature(tool.fn)
        accepted = {k: v for k, v in arguments.items() if k in signature.parameters}
        rejected = sorted(set(arguments) - set(accepted))
        try:
            result = tool.fn(**accepted)
        except Exception as exc:  # surfaced to the model as a tool error
            return {"error": f"{type(exc).__name__}: {exc}"}
        if rejected and isinstance(result, dict):
            result.setdefault("_ignored_arguments", rejected)
        return result

    # --- account lookups (Level 1) --------------------------------------

    def _find_accounts(self, term: str) -> dict[str, Any]:
        matches = self.ws.crm.find_accounts(term)
        return {
            "query": term,
            "count": len(matches),
            "accounts": [_slim_account(a) for a in matches[:20]],
        }

    def _get_account(self, account_id: str) -> dict[str, Any]:
        account = self.ws.crm.get_account(account_id)
        return account or {"error": f"no account with id {account_id}"}

    def _list_contacts(self, account_id: str) -> dict[str, Any]:
        return {"account_id": account_id, "contacts": self.ws.crm.contacts_for(account_id)}

    def _relationship_history(self, account_id: str) -> dict[str, Any]:
        """Everything known about a prior relationship, in one call."""
        account = self.ws.crm.get_account(account_id)
        if account is None:
            return {"error": f"no account with id {account_id}"}
        interactions = self.ws.crm.interactions_for(account_id)
        opportunities = self.ws.crm.opportunities_for(account_id)
        won = [o for o in opportunities if o.get("stage") == PipelineStage.WON.value]
        lost = [o for o in opportunities if o.get("stage") == PipelineStage.LOST.value]
        return {
            "account": _slim_account(account),
            "is_cold": not interactions,
            "interaction_count": len(interactions),
            "recent_interactions": interactions[:10],
            "opportunities_won": len(won),
            "opportunities_lost": len(lost),
            "open_opportunities": [o for o in opportunities if o.get("stage") not in {"won", "lost"}],
            "guidance": (
                "No recorded contact — this is genuinely cold outreach."
                if not interactions
                else "Prior relationship exists; reference it instead of opening cold."
            ),
        }

    def _account_brief(self, account_id: str) -> dict[str, Any]:
        """One-call context pack for outreach or meeting prep."""
        account = self.ws.crm.get_account(account_id)
        if account is None:
            return {"error": f"no account with id {account_id}"}
        segment = str(account.get("account_type", ""))
        return {
            "account": account,
            "contacts": self.ws.crm.contacts_for(account_id),
            "recent_interactions": self.ws.crm.interactions_for(account_id)[:10],
            "opportunities": self.ws.crm.opportunities_for(account_id),
            "signals": self.ws.crm.open_signals_for(str(account.get("name", ""))),
            "relevant_lessons": self.ws.memory.recall(segment=segment, limit=5),
            "reminder": (
                "Personalize only from the fields above. Anything not present here is UNKNOWN."
            ),
        }

    # --- research (Level 1) ---------------------------------------------

    def _search_signals(self, query: str, region: str = "") -> dict[str, Any]:
        web = self.ws.search.search(query=query, count=5)
        tenders = self.ws.construction.tenders(keywords=query, region=region)
        permits = self.ws.construction.permits(region=region or "unspecified")
        connected = web["verified"] or tenders["verified"] or permits["verified"]
        return {
            "query": query,
            "region": region,
            "sources_connected": connected,
            "web": web,
            "tenders": tenders,
            "permits": permits,
            "guidance": (
                "Enrich each result into a signal: who controls the project, is painting "
                "likely, when, who to contact, what to say. Then call record_signal."
                if connected
                else "No research source is connected. Report this as UNKNOWN — do not "
                "present simulated results as market findings."
            ),
        }

    def _research_account(self, company_name: str) -> dict[str, Any]:
        profile = self.ws.construction.company_profile(company_name)
        web = self.ws.search.search(query=f"{company_name} commercial construction projects", count=5)
        return {
            "company": company_name,
            "profile": profile,
            "web": web,
            "sources_connected": profile["verified"] or web["verified"],
        }

    def _score_account(
        self,
        account_id: str,
        days_until_opportunity: int | None = None,
        worked_together_before: bool = False,
    ) -> dict[str, Any]:
        record = self.ws.crm.get_account(account_id)
        if record is None:
            return {"error": f"no account with id {account_id}"}
        account = _to_account(record)
        interactions = [_to_interaction(i) for i in self.ws.crm.interactions_for(account_id)]
        signals = self.ws.crm.open_signals_for(account.name)

        result = score_account(
            account,
            interactions=interactions,
            open_signals=len(signals),
            days_until_opportunity=days_until_opportunity,
            worked_together_before=worked_together_before,
        )
        self.ws.crm.upsert_account(
            {
                **record,
                "score": result.total,
                "score_breakdown": result.breakdown,
                "tier": result.suggested_tier.value,
            }
        )
        return {"account_id": account_id, "name": account.name, **result.to_dict()}

    def _pipeline_report(self) -> dict[str, Any]:
        report = self.ws.crm.pipeline()
        report["open_are_records"] = len(self.ws.memory.open_are_records())
        report["pending_approvals"] = len(self.ws.approvals.pending())
        return report

    def _priority_accounts(self, limit: int = 10) -> dict[str, Any]:
        """The day's work list, tier first then score."""
        records = self.ws.crm.list_accounts()
        scored = []
        for record in records:
            account = _to_account(record)
            interactions = [_to_interaction(i) for i in self.ws.crm.interactions_for(account.id)]
            scored.append(
                (
                    account,
                    score_account(
                        account,
                        interactions=interactions,
                        open_signals=len(self.ws.crm.open_signals_for(account.name)),
                    ),
                )
            )
        ranked = prioritize(scored)[:limit]
        return {
            "count": len(ranked),
            "accounts": [
                {
                    "id": account.id,
                    "name": account.name,
                    "tier": account.tier.value,
                    "stage": account.stage.value,
                    "score": result.total,
                    "next_action": account.next_action or "NONE — this account is unmanaged",
                    "next_action_date": account.next_action_date,
                    "unmanaged": account.unmanaged,
                }
                for account, result in ranked
            ],
        }

    # --- memory (Level 1) -----------------------------------------------

    def _recall_lessons(self, query: str = "", segment: str = "", channel: str = "") -> dict[str, Any]:
        lessons = self.ws.memory.recall(query=query, segment=segment, channel=channel)
        return {
            "count": len(lessons),
            "lessons": lessons,
            "note": "Weigh these by strength and sample size. A single result is not a rule.",
        }

    def _record_lesson(
        self,
        observation: str,
        hypothesis: str,
        evidence: str,
        confidence: str = "inference",
        segment: str = "",
        channel: str = "",
        sample_size: int = 1,
        revenue_outcome: float = 0.0,
        recommended_use: str = "",
        scope: str = "general",
        account_id: str = "",
    ) -> dict[str, Any]:
        lesson = Lesson(
            observation=observation,
            hypothesis=hypothesis,
            evidence=evidence,
            confidence=_confidence(confidence),
            segment=segment,
            channel=channel,
            sample_size=max(1, sample_size),
            revenue_outcome=revenue_outcome,
            recommended_use=recommended_use,
            scope=scope,
            account_id=account_id,
        )
        return self.ws.memory.record_lesson(lesson)

    def _open_are(
        self,
        action: str,
        objective: str,
        target: str,
        expected_result: str,
        measurement: str,
        reasoning: str = "",
        level: str = "action",
        account_id: str = "",
    ) -> dict[str, Any]:
        record = ARERecord(
            action=action,
            objective=objective,
            target=target,
            expected_result=expected_result,
            measurement=measurement,
            reasoning=reasoning,
            level=level,
            account_id=account_id,
        )
        return self.ws.memory.open_are(record)

    def _close_are(
        self,
        are_id: str,
        result: str,
        evaluation: str,
        lesson: str = "",
        confidence: str = "inference",
    ) -> dict[str, Any]:
        return self.ws.memory.close_are(are_id, result, evaluation, lesson, confidence)

    def _propose_improvement(
        self,
        problem: str,
        evidence: str,
        root_cause_hypothesis: str,
        proposed_change: str,
        affected_component: str,
        expected_improvement: str,
        risk: str,
        test_plan: str,
        rollback: str,
    ) -> dict[str, Any]:
        return self.ws.memory.propose(
            ImprovementProposal(
                problem=problem,
                evidence=evidence,
                root_cause_hypothesis=root_cause_hypothesis,
                proposed_change=proposed_change,
                affected_component=affected_component,
                expected_improvement=expected_improvement,
                risk=risk,
                test_plan=test_plan,
                rollback=rollback,
            )
        )

    def _record_signal(
        self,
        headline: str,
        signal_type: str,
        source: str,
        recommended_action: str,
        confidence: str = "inference",
        likely_account: str = "",
        who_controls_it: str = "",
        painting_likely: bool = False,
        likely_timing: str = "",
    ) -> dict[str, Any]:
        if not recommended_action.strip():
            return {
                "error": "a signal without a recommended action is not finished work; "
                "state who to contact and what to say"
            }
        signal = MarketSignal(
            headline=headline,
            signal_type=signal_type,
            source=source,
            confidence=_confidence(confidence),
            likely_account=likely_account,
            who_controls_it=who_controls_it or "unknown",
            painting_likely=painting_likely,
            likely_timing=likely_timing,
            recommended_action=recommended_action,
        )
        return self.ws.crm.record_signal(signal.to_dict())

    # --- estimating support (Level 1) ------------------------------------

    def _estimate_job(
        self,
        job_type: str,
        surfaces: list[dict[str, Any]],
        account_id: str = "",
        crew_size: int = 2,
    ) -> dict[str, Any]:
        try:
            kind = JobType(job_type)
        except ValueError:
            return {"error": f"job_type must be one of {[j.value for j in JobType]}"}
        if not surfaces:
            return {"error": "provide at least one surface with name and square_feet"}

        parsed: list[Surface] = []
        for raw in surfaces:
            try:
                parsed.append(
                    Surface(
                        name=str(raw.get("name", "surface")),
                        square_feet=float(raw["square_feet"]),
                        coats=int(raw.get("coats", 2)),
                        condition=SurfaceCondition(str(raw.get("condition", "good"))),
                        height_feet=float(raw.get("height_feet", 9.0)),
                        needs_primer=bool(raw.get("needs_primer", False)),
                    )
                )
            except (KeyError, ValueError) as exc:
                return {"error": f"bad surface {raw!r}: {exc}"}

        quote = build_quote(lead_id=account_id or "unassigned", job_type=kind, surfaces=parsed)
        data = quote.to_dict()
        data["crew_days"] = estimate_duration_days(quote.labor_hours, crew_size)
        data["disclaimer"] = (
            "Budgetary figure from the internal rate card. Not a quote and not a "
            "price commitment — a human must approve anything sent to a customer."
        )
        self.ws.store.put("quotes", quote.id, data)
        return data

    # --- content (Level 1 draft / Level 3 publish) -----------------------

    def _save_content(
        self,
        kind: str,
        title: str,
        body: str,
        account_id: str = "",
        authorized_details_only: bool = True,
    ) -> dict[str, Any]:
        record = {
            "id": new_id("cnt"),
            "kind": kind,
            "title": title,
            "body": body,
            "account_id": account_id,
            "authorized_details_only": authorized_details_only,
            "status": "draft",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.ws.store.put("content", record["id"], record)
        record["note"] = "Saved as a draft. Publishing requires human approval."
        return record

    def _publish_content(self, content_id: str, channel: str) -> dict[str, Any]:
        record = self.ws.store.get("content", content_id)
        if record is None:
            return {"error": f"no content with id {content_id}"}
        self.ws.store.patch("content", content_id, {"status": "published", "channel": channel})
        return {"content_id": content_id, "channel": channel, "status": "published"}

    # --- CRM writes (Level 2) --------------------------------------------

    def _upsert_account(
        self,
        name: str,
        account_id: str = "",
        account_type: str = "other",
        website: str = "",
        location: str = "",
        size_note: str = "",
        tier: str = "unclassified",
        stage: str = "prospect",
        pain_points: list[str] | None = None,
        likely_needs: list[str] | None = None,
        estimated_annual_value: float = 0.0,
        notes: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        existing = self.ws.crm.get_account(account_id) if account_id else None
        if existing is None and not account_id:
            duplicates = [
                a for a in self.ws.crm.list_accounts()
                if str(a.get("name", "")).strip().lower() == name.strip().lower()
            ]
            if duplicates:
                existing = duplicates[0]

        account = Account(
            name=name,
            account_type=_enum(AccountType, account_type, AccountType.OTHER),
            website=website,
            location=location,
            size_note=size_note,
            tier=_enum(AccountTier, tier, AccountTier.UNCLASSIFIED),
            stage=_enum(PipelineStage, stage, PipelineStage.PROSPECT),
            pain_points=pain_points or [],
            likely_needs=likely_needs or [],
            estimated_annual_value=estimated_annual_value,
            notes=notes,
            source=source,
            id=(existing or {}).get("id") or account_id or new_id("acct"),
        )
        saved = self.ws.crm.upsert_account(account.to_dict())
        saved["_was_update"] = existing is not None
        return saved

    def _upsert_contact(
        self,
        account_id: str,
        name: str,
        title: str = "",
        email: str = "",
        phone: str = "",
        linkedin: str = "",
        is_decision_maker: bool = False,
        notes: str = "",
        contact_id: str = "",
    ) -> dict[str, Any]:
        if self.ws.crm.get_account(account_id) is None:
            return {"error": f"no account with id {account_id}; create the account first"}
        contact = Contact(
            account_id=account_id,
            name=name,
            title=title,
            email=email,
            phone=phone,
            linkedin=linkedin,
            is_decision_maker=is_decision_maker,
            notes=notes,
            id=contact_id or new_id("cont"),
        )
        return self.ws.crm.upsert_contact(contact.to_dict())

    def _log_interaction(
        self,
        account_id: str,
        channel: str,
        direction: str,
        summary: str,
        contact_id: str = "",
        outcome: str = "",
        occurred_on: str = "",
    ) -> dict[str, Any]:
        if self.ws.crm.get_account(account_id) is None:
            return {"error": f"no account with id {account_id}"}
        interaction = Interaction(
            account_id=account_id,
            channel=_enum(Channel, channel, Channel.EMAIL),
            direction=direction if direction in {"inbound", "outbound"} else "outbound",
            summary=summary,
            contact_id=contact_id,
            outcome=outcome,
            occurred_on=occurred_on or today_iso(),
        )
        return self.ws.crm.log_interaction(interaction.to_dict())

    def _set_next_action(self, account_id: str, action: str, due_date: str) -> dict[str, Any]:
        if not action.strip():
            return {"error": "next action cannot be empty — an account without one is unmanaged"}
        return self.ws.crm.set_next_action(account_id, action, due_date or _in_days(7))

    def _advance_stage(self, account_id: str, stage: str) -> dict[str, Any]:
        try:
            target = PipelineStage(stage)
        except ValueError:
            return {"error": f"stage must be one of {[s.value for s in PipelineStage]}"}
        return self.ws.crm.advance_stage(account_id, target)

    def _upsert_opportunity(
        self,
        account_id: str,
        description: str,
        estimated_value: float = 0.0,
        stage: str = "estimating_opportunity",
        probability: float = 0.2,
        expected_decision_date: str = "",
        opportunity_id: str = "",
    ) -> dict[str, Any]:
        if self.ws.crm.get_account(account_id) is None:
            return {"error": f"no account with id {account_id}"}
        opportunity = Opportunity(
            account_id=account_id,
            description=description,
            estimated_value=estimated_value,
            stage=_enum(PipelineStage, stage, PipelineStage.ESTIMATING_OPPORTUNITY),
            probability=min(max(probability, 0.0), 1.0),
            expected_decision_date=expected_decision_date,
            id=opportunity_id or new_id("opp"),
        )
        return self.ws.crm.upsert_opportunity(opportunity.to_dict())

    # --- outbound (Level 2 / Level 3) -------------------------------------

    def _send_email(self, account_id: str, to: str, subject: str, body: str, contact_id: str = "") -> dict[str, Any]:
        if not self.ws.settings.company_email:
            return {"error": "COMPANY_EMAIL is not set; refusing to send from an unknown address"}
        result = self.ws.email.send(
            to=to, subject=subject, body=body, from_email=self.ws.settings.company_email
        )
        if self.ws.crm.get_account(account_id):
            self._log_interaction(
                account_id=account_id,
                channel="email",
                direction="outbound",
                summary=f"Email sent: {subject}",
                contact_id=contact_id,
                outcome="sent",
            )
        result["logged_to_crm"] = bool(self.ws.crm.get_account(account_id))
        return result

    def _schedule_meeting(self, account_id: str, summary: str, meeting_date: str, notes: str = "") -> dict[str, Any]:
        try:
            when = date.fromisoformat(meeting_date)
        except ValueError:
            return {"error": "meeting_date must be ISO format, e.g. 2026-09-14"}
        result = self.ws.calendar.create_event(summary=summary, start=when, days=1, description=notes)
        if self.ws.crm.get_account(account_id):
            self._log_interaction(
                account_id=account_id,
                channel="meeting",
                direction="outbound",
                summary=f"Meeting scheduled: {summary}",
                outcome="meeting_booked",
                occurred_on=meeting_date,
            )
        return result

    # --- registration ----------------------------------------------------

    def _register_all(self) -> None:
        r = self.register

        r(
            "find_accounts",
            "Search the CRM for accounts by company name, contact name, location or note text. "
            "Call this before creating any account so you don't duplicate an existing one.",
            obj({"term": string("Search text, e.g. a company name")}, ["term"]),
            self._find_accounts,
        )
        r(
            "get_account",
            "Fetch one account record by id.",
            obj({"account_id": string("The acct_... id")}, ["account_id"]),
            self._get_account,
        )
        r(
            "list_contacts",
            "List known contacts at an account.",
            obj({"account_id": string("The acct_... id")}, ["account_id"]),
            self._list_contacts,
        )
        r(
            "get_relationship_history",
            "Check whether Ashrah already has a relationship with an account — prior emails, "
            "meetings, estimates, wins and losses. Call this BEFORE any outreach; a warm "
            "relationship outranks cold prospecting.",
            obj({"account_id": string("The acct_... id")}, ["account_id"]),
            self._relationship_history,
        )
        r(
            "build_account_brief",
            "One call that returns everything known about an account: record, contacts, recent "
            "interactions, opportunities, signals and relevant past lessons. Use this to "
            "personalize outreach or prepare for a meeting.",
            obj({"account_id": string("The acct_... id")}, ["account_id"]),
            self._account_brief,
        )
        r(
            "search_market_signals",
            "Search web, tender and permit sources for project signals — new construction, "
            "renovations, tenant improvements, leases, acquisitions, tenders. Returns raw "
            "results for you to enrich into actionable signals.",
            obj(
                {
                    "query": string("What to look for, e.g. 'office tenant improvement Calgary'"),
                    "region": string("Optional region or city to scope permits and tenders"),
                },
                ["query"],
            ),
            self._search_signals,
        )
        r(
            "research_account",
            "Look up a company's profile and recent project activity from external sources.",
            obj({"company_name": string("Company to research")}, ["company_name"]),
            self._research_account,
        )
        r(
            "score_account_tool",
            "Score an account 0-100 on fit, opportunity, value, timing, relationship and "
            "engagement, and save the resulting score and tier. Use this instead of judging "
            "priority by feel.",
            obj(
                {
                    "account_id": string("The acct_... id"),
                    "days_until_opportunity": integer("Days until work could plausibly start, if known"),
                    "worked_together_before": boolean("True if Ashrah has completed work for them"),
                },
                ["account_id"],
            ),
            self._score_account,
        )
        r(
            "priority_accounts",
            "The work list for this cycle: accounts ranked by tier then score, flagging any "
            "that have no next action. Start a daily loop here.",
            obj({"limit": integer("How many accounts to return (default 10)")}),
            self._priority_accounts,
        )
        r(
            "pipeline_report",
            "Pipeline rollup: accounts by stage and tier, open opportunities, pipeline value, "
            "weighted value, unmanaged accounts and overdue next actions.",
            obj({}),
            self._pipeline_report,
        )
        r(
            "recall_lessons",
            "Retrieve past marketing lessons before designing outreach or a campaign, so you "
            "don't repeat something the evidence already says doesn't work.",
            obj(
                {
                    "query": string("What you're deciding, e.g. 'first contact property manager'"),
                    "segment": string("Account type filter", [t.value for t in AccountType]),
                    "channel": string("Channel filter", [c.value for c in Channel]),
                }
            ),
            self._recall_lessons,
        )
        r(
            "record_lesson",
            "Store a durable finding in marketing memory. Repeated findings merge and raise the "
            "sample size — one interaction is not a rule.",
            obj(
                {
                    "observation": string("What objectively happened"),
                    "hypothesis": string("What may explain it"),
                    "evidence": string("What supports the hypothesis"),
                    "confidence": string("Confidence label", [c.value for c in Confidence]),
                    "segment": string("Account type this applies to", [t.value for t in AccountType]),
                    "channel": string("Channel this applies to", [c.value for c in Channel]),
                    "sample_size": integer("How many cases this is based on"),
                    "revenue_outcome": number("Revenue attributable to this, if any"),
                    "recommended_use": string("When to apply this in future"),
                    "scope": string("'general' learning or account-specific 'preference'", ["general", "preference"]),
                    "account_id": string("Account id when scope is 'preference'"),
                },
                ["observation", "hypothesis", "evidence"],
            ),
            self._record_lesson,
        )
        r(
            "open_are_record",
            "Open an ARE record before a significant action: objective, target, expected result "
            "and how success will be measured.",
            obj(
                {
                    "action": string("What you are about to do"),
                    "objective": string("What it should accomplish"),
                    "target": string("Who or what you are acting on"),
                    "expected_result": string("What should happen if it works"),
                    "measurement": string("How success or failure will be determined"),
                    "reasoning": string("Why this action, now, through this channel"),
                    "level": string("Scope of the loop", ["action", "campaign", "strategic"]),
                    "account_id": string("Related account id, if any"),
                },
                ["action", "objective", "target", "expected_result", "measurement"],
            ),
            self._open_are,
        )
        r(
            "close_are_record",
            "Close an ARE record once evidence exists: what happened, why it probably happened, "
            "what was learned, and your confidence.",
            obj(
                {
                    "are_id": string("The are_... id"),
                    "result": string("What actually happened"),
                    "evaluation": string("Why it probably happened and what evidence supports that"),
                    "lesson": string("What should change next time"),
                    "confidence": string("Confidence label", [c.value for c in Confidence]),
                },
                ["are_id", "result", "evaluation"],
            ),
            self._close_are,
        )
        r(
            "record_signal",
            "Record a market signal enriched into an action. A recommended_action is required — "
            "a signal without one is not finished work.",
            obj(
                {
                    "headline": string("The event, e.g. 'Permit issued for 40k sqft warehouse fit-out'"),
                    "signal_type": string("permit, tender, lease, renovation, acquisition, award, other"),
                    "source": string("Where this came from"),
                    "recommended_action": string("Who to contact, what to say, and when"),
                    "confidence": string("Confidence label", [c.value for c in Confidence]),
                    "likely_account": string("Company this points to"),
                    "who_controls_it": string("GC, property manager or owner controlling the work"),
                    "painting_likely": boolean("Whether painting scope is plausible"),
                    "likely_timing": string("When painting would occur"),
                },
                ["headline", "signal_type", "source", "recommended_action"],
            ),
            self._record_signal,
        )
        r(
            "propose_improvement",
            "File a Lumia Improvement Proposal when evidence shows part of the system itself is "
            "underperforming. Proposals are reviewed by a human; they do not self-apply.",
            obj(
                {
                    "problem": string("What is not working"),
                    "evidence": string("Which ARE results demonstrate it"),
                    "root_cause_hypothesis": string("Why this may be happening"),
                    "proposed_change": string("What should change"),
                    "affected_component": string(
                        "Component", ["prompt", "workflow", "agent", "tool", "memory", "crm", "evaluation", "dataset"]
                    ),
                    "expected_improvement": string("What should improve"),
                    "risk": string("What could go wrong"),
                    "test_plan": string("How the change should be evaluated"),
                    "rollback": string("How to restore the previous version"),
                },
                [
                    "problem",
                    "evidence",
                    "root_cause_hypothesis",
                    "proposed_change",
                    "affected_component",
                    "expected_improvement",
                    "risk",
                    "test_plan",
                    "rollback",
                ],
            ),
            self._propose_improvement,
        )
        r(
            "estimate_paint_job",
            "Produce a budgetary figure from Ashrah's internal rate card for scoping "
            "conversations. This is not a quote and must never be sent as a price commitment.",
            obj(
                {
                    "job_type": string("Type of work", [j.value for j in JobType]),
                    "surfaces": {
                        "type": "array",
                        "description": "Areas to paint",
                        "items": obj(
                            {
                                "name": string("What this surface is"),
                                "square_feet": number("Paintable square footage"),
                                "coats": integer("Coats, default 2"),
                                "condition": string("Surface condition", [c.value for c in SurfaceCondition]),
                                "height_feet": number("Working height; over 10ft adds staging time"),
                                "needs_primer": boolean("Whether primer is required"),
                            },
                            ["name", "square_feet"],
                        ),
                    },
                    "account_id": string("Related account id"),
                    "crew_size": integer("Crew size for the duration estimate, default 2"),
                },
                ["job_type", "surfaces"],
            ),
            self._estimate_job,
        )
        r(
            "save_content",
            "Save a marketing asset as a draft — case study, capability statement, post or "
            "sales enablement material. Drafting is autonomous; publishing is not.",
            obj(
                {
                    "kind": string("Asset type", ["case_study", "capability_statement", "post", "email_template", "brief"]),
                    "title": string("Asset title"),
                    "body": string("Full text"),
                    "account_id": string("Related account id, if any"),
                    "authorized_details_only": boolean("Confirm no unauthorized client detail is included"),
                },
                ["kind", "title", "body"],
            ),
            self._save_content,
        )
        r(
            "publish_content",
            "Publish a saved draft externally. Requires human approval.",
            obj(
                {
                    "content_id": string("The cnt_... id"),
                    "channel": string("Where to publish", ["website", "linkedin", "email_campaign"]),
                },
                ["content_id", "channel"],
            ),
            self._publish_content,
        )
        r(
            "upsert_account",
            "Create or update an account. Search with find_accounts first to avoid duplicates.",
            obj(
                {
                    "name": string("Company name"),
                    "account_id": string("Existing acct_... id when updating"),
                    "account_type": string("Segment", [t.value for t in AccountType]),
                    "website": string("Website"),
                    "location": string("City / region"),
                    "size_note": string("Scale, e.g. '~40 properties'"),
                    "tier": string("Tier", [t.value for t in AccountTier]),
                    "stage": string("Pipeline stage", [s.value for s in PipelineStage]),
                    "pain_points": array("Known problems they have"),
                    "likely_needs": array("Painting work they plausibly need"),
                    "estimated_annual_value": number("Estimated annual revenue potential"),
                    "notes": string("Free-text notes"),
                    "source": string("Where this account came from"),
                },
                ["name"],
            ),
            self._upsert_account,
        )
        r(
            "upsert_contact",
            "Create or update a decision-maker at an account. Only record contact details you "
            "actually retrieved — never guess an email address.",
            obj(
                {
                    "account_id": string("The acct_... id"),
                    "name": string("Person's name"),
                    "title": string("Job title"),
                    "email": string("Business email, only if verified"),
                    "phone": string("Phone, only if verified"),
                    "linkedin": string("LinkedIn URL"),
                    "is_decision_maker": boolean("Whether they control painting decisions"),
                    "notes": string("Context about this person"),
                    "contact_id": string("Existing cont_... id when updating"),
                },
                ["account_id", "name"],
            ),
            self._upsert_contact,
        )
        r(
            "log_interaction",
            "Record a real interaction with an account. Log only what actually happened.",
            obj(
                {
                    "account_id": string("The acct_... id"),
                    "channel": string("Channel", [c.value for c in Channel]),
                    "direction": string("Direction", ["outbound", "inbound"]),
                    "summary": string("What was said or happened"),
                    "contact_id": string("Contact involved"),
                    "outcome": string(
                        "Result",
                        [
                            "sent", "no_response", "positive_reply", "negative_reply",
                            "meeting_booked", "estimate_requested", "tender_invitation",
                            "vendor_registration", "referral", "won", "lost", "unsubscribed",
                        ],
                    ),
                    "occurred_on": string("ISO date, defaults to today"),
                },
                ["account_id", "channel", "direction", "summary"],
            ),
            self._log_interaction,
        )
        r(
            "set_next_action",
            "Set the next action and due date on an account. Every active account must have one.",
            obj(
                {
                    "account_id": string("The acct_... id"),
                    "action": string("The specific next step, with a reason"),
                    "due_date": string("ISO date it should happen by"),
                },
                ["account_id", "action"],
            ),
            self._set_next_action,
        )
        r(
            "advance_stage",
            "Move an account to a different pipeline stage when the evidence supports it.",
            obj(
                {
                    "account_id": string("The acct_... id"),
                    "stage": string("New stage", [s.value for s in PipelineStage]),
                },
                ["account_id", "stage"],
            ),
            self._advance_stage,
        )
        r(
            "upsert_opportunity",
            "Create or update a specific revenue opportunity on an account.",
            obj(
                {
                    "account_id": string("The acct_... id"),
                    "description": string("What the work is"),
                    "estimated_value": number("Estimated value in dollars"),
                    "stage": string("Stage", [s.value for s in PipelineStage]),
                    "probability": number("Probability of winning, 0 to 1"),
                    "expected_decision_date": string("ISO date a decision is expected"),
                    "opportunity_id": string("Existing opp_... id when updating"),
                },
                ["account_id", "description"],
            ),
            self._upsert_opportunity,
        )
        r(
            "send_followup_email",
            "Send a follow-up email to an existing relationship and log it. Use only where "
            "prior contact exists and the message carries genuine context or value.",
            obj(
                {
                    "account_id": string("The acct_... id"),
                    "to": string("Recipient email address"),
                    "subject": string("Subject line"),
                    "body": string("Full message body"),
                    "contact_id": string("Contact being emailed"),
                },
                ["account_id", "to", "subject", "body"],
            ),
            self._send_email,
        )
        r(
            "send_first_contact_email",
            "Send a first-contact email to a new account. Requires human approval before it "
            "goes out.",
            obj(
                {
                    "account_id": string("The acct_... id"),
                    "to": string("Recipient email address"),
                    "subject": string("Subject line"),
                    "body": string("Full message body"),
                    "contact_id": string("Contact being emailed"),
                },
                ["account_id", "to", "subject", "body"],
            ),
            self._send_email,
        )
        r(
            "schedule_meeting",
            "Put a meeting or site walk on the calendar and log it against the account.",
            obj(
                {
                    "account_id": string("The acct_... id"),
                    "summary": string("Meeting title"),
                    "meeting_date": string("ISO date"),
                    "notes": string("Agenda or context"),
                },
                ["account_id", "summary", "meeting_date"],
            ),
            self._schedule_meeting,
        )


# --- helpers -------------------------------------------------------------


def _enum(enum_cls: Any, value: str, fallback: Any) -> Any:
    try:
        return enum_cls(value)
    except ValueError:
        return fallback


def _confidence(value: str) -> Confidence:
    return _enum(Confidence, value, Confidence.INFERENCE)


def _in_days(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _slim_account(record: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "name", "account_type", "tier", "stage", "score", "next_action", "next_action_date", "location")
    return {k: record.get(k) for k in keys}


def _to_account(record: dict[str, Any]) -> Account:
    return Account(
        name=str(record.get("name", "")),
        account_type=_enum(AccountType, str(record.get("account_type", "other")), AccountType.OTHER),
        website=str(record.get("website", "")),
        location=str(record.get("location", "")),
        size_note=str(record.get("size_note", "")),
        tier=_enum(AccountTier, str(record.get("tier", "unclassified")), AccountTier.UNCLASSIFIED),
        stage=_enum(PipelineStage, str(record.get("stage", "prospect")), PipelineStage.PROSPECT),
        pain_points=list(record.get("pain_points", []) or []),
        likely_needs=list(record.get("likely_needs", []) or []),
        estimated_annual_value=float(record.get("estimated_annual_value", 0) or 0),
        score=int(record.get("score", 0) or 0),
        next_action=str(record.get("next_action", "")),
        next_action_date=str(record.get("next_action_date", "")),
        last_interaction_date=str(record.get("last_interaction_date", "")),
        notes=str(record.get("notes", "")),
        source=str(record.get("source", "")),
        id=str(record.get("id", new_id("acct"))),
    )


def _to_interaction(record: dict[str, Any]) -> Interaction:
    return Interaction(
        account_id=str(record.get("account_id", "")),
        channel=_enum(Channel, str(record.get("channel", "email")), Channel.EMAIL),
        direction=str(record.get("direction", "outbound")),
        summary=str(record.get("summary", "")),
        contact_id=str(record.get("contact_id", "")),
        outcome=str(record.get("outcome", "")),
        occurred_on=str(record.get("occurred_on", today_iso())),
        id=str(record.get("id", new_id("intx"))),
    )
