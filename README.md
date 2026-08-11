# Lumia

**The operating system for Ashrah Painting Ltd. — winning the work, and
communicating it.**

Lumia has two halves that share one harness:

- **Lumia Marketing** is a growth operator: it turns market intelligence
  into target accounts, accounts into relationships, relationships into
  estimates, and estimates into revenue.
- **The Lumia Communication Agent** takes over once the work is won: it
  turns what the field submits — in any language, by voice or photo — into
  accurate, professional communication to clients, general contractors,
  crews and suppliers, with a record behind every message.

Both run on Claude Opus 5 with a harness that enforces the parts you cannot
leave to a prompt: autonomy levels, an approval gate, deterministic scoring
and screening, and a memory that accumulates evidence.

```bash
git clone https://github.com/ahmadashrah/ashrah-painting-platform-test-1998.git
cd ashrah-painting-platform-test-1998
pip install -r requirements-dev.txt
cp .env.example .env          # add your ANTHROPIC_API_KEY

export PYTHONPATH=src
python -m lumia.cli status         # what's live vs mocked
python -m lumia.cli seed           # load demo accounts
python -m lumia.cli daily          # run today's growth cycle

python -m lumia.cli comms seed     # load a demo project
python -m lumia.cli comms intake   # process today's field reports
python -m lumia.cli comms daily-log
```

Everything runs without a single third-party credential. Unconfigured
integrations return simulated data that is **explicitly flagged**, and
Lumia is instructed to report those as UNKNOWN rather than as findings —
so a demo can never be mistaken for a real market read.

---

## The Communication Agent

A crew lead sends a voice note in Arabic at 2am. By morning the client has a
professional daily log — or, more usefully, a specific question, because the
hours in that voice note did not match the clock records.

```bash
python -m lumia.cli comms seed        # a demo project with a deliberately imperfect day
python -m lumia.cli comms intake      # transcribe, translate, normalize, cross-check
python -m lumia.cli comms daily-log   # compose and send the client log
python -m lumia.cli comms dispatch    # tell each crew where to be
python -m lumia.cli comms followups   # chase unanswered questions and failed deliveries
python -m lumia.cli comms review --days 7 --metrics-only
python -m lumia.cli comms run "the super wants to know about tomorrow's access"
```

**The original is never overwritten.** A field submission keeps its raw
text, its language, its transcript and its translation as separate fields,
and every change appends to a revision trail. `revise_submission` refuses to
touch the original at all — corrections are recorded, not applied in place.
The client reads Ashrah's report; nothing in it reveals that the source was
a rough note in another language.

**Conflicts are found, not smoothed over.** Every submission is
mechanically compared against the platform record: reported hours against
clock-ins, headcount against who actually badged in, reported work against
the contract's exclusions, today's report against what earlier logs already
claimed complete. A conflicted submission cannot be composed into a client
log at all.

```
Reported 20.0 labour hours but clock records show 15.5.
Reported work overlaps a scope exclusion ('Ceiling painting is excluded
from the contract.') on ceiling. This may be extra work — escalate before
reporting it as contract scope.
```

The corroborating data is platform-owned. **No tool on the agent's surface
can write a time record**, so the agent cannot manufacture agreement with
itself.

**Photos carry their own restrictions.** A caption claiming completion is
rejected — a photo shows a state, it cannot prove work is finished. Flags
for personal information, unsafe conduct, security systems or confidential
documents force `client_safe` to false regardless of what was requested, and
a flagged photo cannot be attached to an external message.

**Recipients are resolved, never composed.** There is no parameter anywhere
for a free-text email address. Every message resolves its recipient against
the project's recorded contacts, or it refuses.

---

## Sending is gated on what the message actually says

Preparing and sending are different tools. `draft_communication` only writes
a record; `send_communication` takes a draft id, and the body it screens and
transmits is the **stored** one — so a message cannot be screened clean and
then sent as something else.

Three gates, all of which must pass:

| Gate | Rule |
|---|---|
| **Recipient** | resolved against the project's contacts |
| **Kind** | on the approved routine list — daily logs, schedule reminders, arrival notices, confirmation and clarification requests, crew dispatches |
| **Content** | survives the screen |

The screen is `comms/screening.py`, and it is the mechanism behind the
spec's Level 3 list: pricing, change orders, contractual commitments,
completion guarantees, schedule commitments, admissions of fault, disputes,
legal and insurance matters, safety incidents, discipline, termination,
refunds, serious complaints, uncertainty, and internal information leaking
to an external recipient.

```
$ a price sneaks into an otherwise routine daily log
Level 3 — Message content requires human approval — pricing ('$4').

$ an arrival notice claims a floor is finished
Level 3 — Message content requires human approval — completion_guarantee ('100% complete').
```

It is deliberately biased toward false positives. A needless approval prompt
costs a manager seconds; a missed one sends a price or an admission of
liability to a general contractor in Ashrah's name.

Two refinements matter. Screening is **recipient-aware**: uncertainty and
confidential information gate only on the way out of the company, because
asking a crew lead "confirm whether the corridor got primer only" is the
clarification workflow working, not a risk. And it understands **negation**
where the spec is about an occurrence — "no injuries today" is a normal log
line — but never for liability: "we are not responsible for the damage" is
as much a legal statement as admitting it, and both are gated.

**Escalation is never gated.** Telling a manager about an injury, damage, a
dispute or a potential change order is Level 2 and goes immediately. It just
has to be complete: an escalation without evidence, impact and a recommended
next action is rejected.

---

## The operating loop

Every meaningful action passes through **ARE — Action → Reasoning →
Evaluation**.

```
ACTION → RESULT → REASONING → EVALUATION → LEARNING → STRATEGY UPDATE → NEXT ACTION
```

An action declares its objective, target, expected result and how success
will be measured. When evidence arrives, the record is closed with what
happened, why, and how confident that conclusion is. Findings that repeat
merge into a single lesson with a growing sample size, so one good email
never becomes a permanent rule.

```bash
python -m lumia.cli daily --focus "general contractors"
python -m lumia.cli weekly --days 7
python -m lumia.cli weekly --metrics-only     # numbers only, no model call
```

The daily cycle runs review → prioritize → research → plan → execute →
record → learn. The weekly review reports pipeline, activity, conversion
and intelligence, then names the five highest-impact actions for next
week.

---

## Autonomy levels are enforced in code

The spec defines three levels. This repo makes them real: every tool is
classified, and the agent loop physically cannot execute a Level 3 call.

| Level | Examples | Behaviour |
|---|---|---|
| **1 — Autonomous** | research, scoring, reporting, CRM reads; transcription, translation, verification, drafting | runs freely |
| **2 — Controlled** | CRM and project writes, routine follow-ups, scheduling, routine sends, escalation to management | runs within approved rules |
| **3 — Approval required** | first contact, publishing, pricing commitments, material orders, any message that trips the content screen | **queued for a human** |

Classification is *dynamic*, and it escalates on two different things.

On the growth side it escalates on **who**: a follow-up email is Level 2
until its target is a Tier A account that has never been contacted, or an
account worth more than $150k a year.

On the communication side it escalates on **what the message says**: the
gate loads the stored draft, checks its kind against the approved routine
list, and screens its actual body. A daily log is Level 2 until a price
appears in it.

An unrecognised tool defaults to Level 3: unknown means dangerous, not safe.
A test asserts that every registered tool is classified, so nothing lands in
that default by accident.

When an action is gated, the agent is told plainly that it did **not**
happen, so it reports the truth instead of claiming success:

```bash
python -m lumia.cli approvals list
python -m lumia.cli approvals approve appr_abc123 --note "ok" --execute
python -m lumia.cli approvals reject  appr_abc123 --note "wrong contact"
```

---

## Anti-hallucination, structurally

The rule "never invent contacts, projects or relationships" is backed by
mechanism, not just instruction:

- **Provenance flags.** Unconfigured integrations return `_mocked: true`
  and a reason. The prompt names exactly which sources are live in this
  environment.
- **Evidence labels.** Every claim is KNOWN FACT, INFERENCE or UNKNOWN.
- **Relationship-first.** `get_relationship_history` must be checked
  before outreach; it returns `is_cold` so a warm account is never
  cold-opened.
- **Tool-enforced completeness.** `record_signal` rejects a signal with no
  recommended action — "Company X announced a new building" is a note, not
  an output. `set_next_action` rejects a blank action.
- **Estimates are labelled.** The estimating tool returns a budgetary
  figure stamped *not a quote, not a price commitment*.
- **Reports are built from corroborated facts.** `build_daily_log` returns
  what the platform can verify plus an explicit list of gaps, and
  `compose_daily_log` refuses a log with no verified work in it, an
  unverified completion claim, or an uncleared photo.
- **Rates are null, not zero.** A delivery success rate of `0.0` means every
  message failed; `None` means none were sent. The metrics never report the
  second as the first, and `pending_processing` is stated alongside the
  accuracy rates so an untouched backlog cannot read as a clean week.

---

## Architecture

```
src/lumia/
├── prompts.py        the growth operating spec; picks the core per role
├── agent.py          the tool loop — where the autonomy gate intercepts
├── autonomy.py       Level 1/2/3 classification + approval queue
├── orchestrator.py   growth routing, the daily cycle, the weekly review
├── tools.py          the growth tool surface
├── memory.py         ARE ledger + learning memory
├── reporting.py      weekly growth metrics (computed, never narrated)
├── workspace.py      wiring: config → store → integrations
├── schema.py         JSON-Schema builders shared by both tool surfaces
├── agents/           role prompts and per-role tool allowlists
├── comms/
│   ├── prompts.py       the communication operating spec
│   ├── tools.py         the communication tool surface (mixed into Toolbox)
│   ├── screening.py     content risk → the Level 3 trigger, in code
│   ├── verification.py  field report vs scope, clock records and prior logs
│   ├── dailylog.py      assembly, validation and rendering
│   ├── channels.py      email vs text, decided deterministically
│   ├── ledger.py        the communication record of truth, append-only
│   ├── desk.py          intake, daily logs, dispatch, follow-ups, review
│   └── reporting.py     communication performance metrics
├── domain/
│   ├── accounts.py   accounts, contacts, interactions, opportunities, signals
│   ├── projects.py   projects, crew, submissions, media, logs, messages
│   ├── scoring.py    the six-factor lead score and tiering
│   └── pricing.py    the estimating rate card
└── integrations/     CRM, email, SMS, calendar, search, permits, weather
```

**Specialists.** Ten roles, each with a restricted toolset and the operating
spec that matches its job.

| Growth | Communication |
|---|---|
| `director`, `research`, `outreach`, `content`, `crm` | `intake`, `client_comms`, `crew_comms`, `vendor_comms`, `escalation` |

Restriction does real work. The Content agent cannot email a prospect; the
Client Reporting agent cannot order materials; only `vendor_comms` holds
`place_material_order` — and that one still needs a human. Every
communication role can escalate.

```bash
python -m lumia.cli run "find property managers in Calgary with turnovers coming"
python -m lumia.cli run "draft a case study from the Riverbend job" --role content
python -m lumia.cli comms run "translate the voice note from the crew lead" --role intake
```

**Why scoring and pricing are plain Python.** The same account with the
same evidence must always produce the same score, and a budgetary figure
must be reproducible and auditable. The model decides *what evidence
exists*; the code turns evidence into a number.

Lead score is 100 points across the six factors from the spec — fit (20),
opportunity (20), value (20), timing (15), relationship (15), engagement
(10) — and tier follows from score and annual value.

---

## Adding an integration

1. Subclass `Integration` in `src/lumia/integrations/`. Call
   `self.request(...)` with a `mock=` payload for the unconfigured path.
2. Register credentials in `config.py`.
3. Add it to `Workspace.build`.
4. Add a tool in `tools.py` (growth) or `comms/tools.py` (project
   communication) with a description that says **when** to call it, and
   classify it in `autonomy.py`.
5. Add it to the allowlists of the roles that should hold it, in
   `agents/__init__.py`.

Step 4 is not optional: a tool missing from the autonomy table is treated
as Level 3 and will be gated on every call. `test_every_registered_tool_is_classified`
fails if you forget.

---

## Development

```bash
python -m pytest -q          # 139 tests, no network, no API key needed
```

The suite drives the agent loop with a scripted fake client, so both gates,
the CRM round trip, scoring, pricing, screening, verification, the daily-log
renderer and the reporting maths are all covered without spending a token.

**Demo data.** `lumia seed` writes three fictional accounts and
`lumia comms seed` a fictional project, all tagged `source: "demo_seed"` and
named `(DEMO)`. The project seed deliberately contains an imperfect day — an
Arabic voice report whose hours contradict the clock records, work that
touches a contract exclusion, and a photo of a security keypad — because a
demo where everything reconciles shows none of the behaviour that matters.
Delete `data/` before pointing this at real work.

**State.** All local state lives in `data/` (git-ignored): `lumia.json`
holds the CRM, the project record and memory; `approvals.json` the pending
gate queue. Point `ASHRAH_DATA_DIR` elsewhere to isolate environments.

---

## Configuration

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | required to run the agent |
| `ASHRAH_MODEL` | defaults to `claude-opus-5` |
| `ASHRAH_EFFORT` | `low`–`max`, defaults to `high` |
| `COMPANY_EMAIL` | outbound email is refused without it |
| `CRM_API_KEY` | blank uses the built-in local store |
| `SENDGRID_API_KEY` | outreach and project email |
| `TWILIO_*` | site coordination by text — all three vars required |
| `SEARCH_API_KEY` | market research |
| `CONSTRUCTION_DATA_API_KEY` | permits, tenders, project awards |
| `GOOGLE_CALENDAR_TOKEN` | meetings and site walks |

See `.env.example` for the full list.

---

## A deliberately unfinished system

Lumia is versioned, not finished. When evidence shows part of the system
itself is underperforming, the agent files a **Lumia Improvement
Proposal** — problem, evidence, root-cause hypothesis, proposed change,
expected improvement, risk, test plan and rollback. Proposals are stored
for human review; they never self-apply. Lumia may observe, measure,
evaluate and recommend. It may not rewrite its own permissions, approval
gates or safety rules.

Observe → Evaluate → Recommend → Test when authorized → Measure → Adopt or reject.
