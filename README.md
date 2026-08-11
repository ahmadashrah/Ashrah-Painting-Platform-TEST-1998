# Lumia

**The agentic operating system for Ashrah Painting Ltd.**

Lumia covers both halves of the business:

- **Growth** — turning market intelligence into target accounts, accounts
  into relationships, relationships into estimates, and estimates into
  revenue.
- **Delivery** — turning won work into a crew schedule that is actually
  achievable, and keeping it honest as reality moves.

Both run on Claude Opus 5 with a harness that enforces the parts you cannot
leave to a prompt: autonomy levels, an approval gate, deterministic scoring
and constraint checking, and a memory that accumulates evidence.

```bash
git clone https://github.com/ahmadashrah/ashrah-painting-platform-test-1998.git
cd ashrah-painting-platform-test-1998
pip install -r requirements-dev.txt
cp .env.example .env          # add your ANTHROPIC_API_KEY

export PYTHONPATH=src
python -m lumia.cli status          # what's live vs mocked
python -m lumia.cli seed            # load demo accounts, crew and projects
python -m lumia.cli daily           # today's growth cycle
python -m lumia.cli schedule        # this week's crew schedule
python -m lumia.cli monitor         # what's about to go wrong
```

Everything runs without a single third-party credential. Unconfigured
integrations return simulated data that is **explicitly flagged**, and
Lumia is instructed to report those as UNKNOWN rather than as findings —
so a demo can never be mistaken for a real market read.

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

## The Scheduling Agent

The delivery side is an ARE-oriented operational agent, not a calendar
assistant. It analyses projects and workforce data, reasons about
constraints, recommends crews, executes what it is authorized to execute,
monitors what actually happened, and improves the next estimate from the
variance.

```bash
python -m lumia.cli schedule --start 2026-08-17 --focus "Riverbend deadline"
python -m lumia.cli schedule --plan-only     # the proposed week, no model call
python -m lumia.cli monitor --report-only    # ranked risks, no model call
python -m lumia.cli roster --date 2026-08-17 # who's free, and why the rest aren't
```

**The rules are mechanism, not instruction.** The spec's "you must never"
list is enforced in `scheduling.py` before anything is written, and the
verdict comes in three tiers that behave differently:

| Tier | Examples | Behaviour |
|---|---|---|
| **Block** | double-booking, the 12h/day and 60h/week safety caps, a shift on a cancelled project | **no approval path** — refused, change the plan |
| **Exception** | scheduling over approved time off, assigning past a certification, overtime beyond 44h | queued for a human via `request_scheduling_exception` |
| **Warning** | skill gaps, a cert expiring soon, travel from yesterday's site, tentative project | travels with the result, never blocks |

That middle tier is the interesting one. `assign_crew` refuses an exception
and *names the decision*; the exception is a separate Level 3 tool. The
agent cannot argue its way past a constraint, because the constraint does
not live in the prompt.

Approvals are carried by an `approval_id` that only the harness can supply
— `Toolbox.call` overwrites the field, so a model that puts one in its own
arguments is ignored. And an approval still cannot create a blocked shift:
a human authorizing a double-booking gets the same refusal.

**Draft → confirmed → notified.** A draft is a proposal, a confirmed shift
is a commitment, and only a briefed crew is actually scheduled.
`send_crew_schedule` refuses to brief anyone on a draft, and every shift is
re-checked at the moment of confirmation in case the world moved since the
draft was written.

**Crew matching is deterministic**, for the same reason scoring and pricing
are: an assignment an employee questions has to be explainable from a
breakdown. 100 points across skill match (35), productivity (20),
reliability (20), site familiarity (15) and leadership (10). Certifications
are deliberately *not* scored — points can be traded off, a fall-protection
card cannot, so a missing or expired one is a blocker.

**Painting sequence is real.** Projects break into the thirteen production
phases with dependencies and cure windows, so a final coat cannot be
scheduled into a recoat window and a phase whose predecessor is unfinished
reports as blocked rather than being quietly staffed.

**Messages are composed by the tools, not the model.** A crew briefing that
omits the address or the lead is an operational failure; a client note that
leaks labour cost is a commercial one. Both are assembled from structured
fields — `notify_client_schedule_change` has no free-text parameter at all,
so internal costs and employee data have no channel to travel out through.

---

## Autonomy levels are enforced in code

The spec defines three levels. This repo makes them real: every tool is
classified, and the agent loop physically cannot execute a Level 3 call.

| Level | Examples | Behaviour |
|---|---|---|
| **1 — Autonomous** | research, scoring, drafting, reporting, CRM reads, risk reports, crew proposals | runs freely |
| **2 — Controlled** | CRM writes, routine follow-ups, creating and confirming shifts, crew briefings | runs within approved rules |
| **3 — Approval required** | first contact, publishing, pricing commitments, scheduling exceptions, postponing work, client schedule changes, subcontractors | **queued for a human** |

Classification is also *dynamic*. A follow-up email is Level 2 — until its
target is a Tier A account that has never been contacted, or an account
worth more than $150k a year, at which point it escalates to Level 3
automatically. An unrecognised tool defaults to Level 3: unknown means
dangerous, not safe.

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
- **Attendance is never invented.** An unconfigured timeclock returns *no
  entries plus a reason*, not plausible-looking punches — fabricating
  attendance in a demo is the same lie as fabricating it in production.
  Project actual hours are recomputed from recorded entries; there is no
  tool that accepts an hours figure directly.
- **Schedules are labelled by state.** A plan is stamped `provisional`, a
  shift is `draft` until confirmed, and the tools refuse to brief a crew on
  anything unconfirmed.

---

## Architecture

```
src/lumia/
├── prompts.py            both operating specs, versioned and composable
├── agent.py              the tool loop — where the autonomy gate intercepts
├── autonomy.py           Level 1/2/3 classification + approval queue
├── orchestrator.py       routing, daily cycle, weekly review, scheduling cycle
├── tools.py              the growth tool surface + the shared registry
├── scheduling.py         the scheduling engine — constraints, plans, risk
├── scheduling_tools.py   the delivery tool surface
├── memory.py             ARE ledger + learning memory
├── reporting.py          weekly metrics (computed, never narrated)
├── workspace.py          wiring: config → store → integrations
├── agents/               role prompts and per-role tool allowlists
├── domain/
│   ├── accounts.py       accounts, contacts, interactions, opportunities, signals
│   ├── scoring.py        the six-factor lead score and tiering
│   ├── pricing.py        the estimating rate card
│   ├── workforce.py      people, skills, certifications, availability, shifts
│   ├── projects.py       projects, the thirteen phases, dependencies, priority
│   └── crewing.py        the five-factor crew fit score
└── integrations/         CRM, operations, timeclock, email, SMS, calendar,
                          search, permits, weather
```

**Specialists.** Six roles get a restricted toolset: `director`,
`research`, `outreach`, `content`, `crm` and `scheduler`. Requests are
auto-routed. The Content agent cannot email a prospect because it does not
hold the tool, and the Scheduling Agent holds no outreach tools at all —
moving a crew and emailing a prospect are different jobs.

```bash
python -m lumia.cli run "find property managers in Calgary with turnovers coming"
python -m lumia.cli run "draft a case study from the Riverbend job" --role content
python -m lumia.cli run "who is on the Riverbend crew tomorrow"
```

The two halves meet at the project record: a project carries the
`account_id` of the CRM account that won it, so delivery performance is
traceable back to the relationship that produced it.

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
4. Add a tool in `tools.py` with a description that says **when** to call
   it, and classify it in `autonomy.py`.

Step 4 is not optional: a tool missing from the autonomy table is treated
as Level 3 and will be gated on every call.

---

## Development

```bash
python -m pytest -q          # 93 tests, no network, no API key needed
```

The suite drives the agent loop with a scripted fake client, so the gate,
the CRM round trip, scoring, pricing, the reporting maths and every
scheduling constraint are covered without spending a token. Each scheduling
test corresponds to a rule the agent is not trusted to follow on its own —
including that a model-supplied `approval_id` is discarded, and that an
approval cannot create a double-booking.

**Demo data.** `lumia seed` writes three fictional accounts, five crew
members and three projects, all named `(DEMO)`. The dataset deliberately
contains live problems — a lapsed boom-lift certification, one expiring
within the month, and approved vacation across a week a deadline needs —
because a seed where nothing is wrong cannot demonstrate a risk report. Run
`lumia monitor --report-only` to see them ranked. Delete `data/` before
pointing this at real records.

**State.** All local state lives in `data/` (git-ignored): `lumia.json`
holds the CRM, operations records and memory, `approvals.json` the pending
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
| `SENDGRID_API_KEY` | outreach email |
| `SEARCH_API_KEY` | market research |
| `CONSTRUCTION_DATA_API_KEY` | permits, tenders, project awards |
| `GOOGLE_CALENDAR_TOKEN` | meetings and site walks |
| `OPERATIONS_API_KEY` | hosted projects/crew/shift system; blank uses the local store |
| `TIMECLOCK_API_KEY` | clock-in/out; blank means attendance is UNKNOWN |
| `OPENWEATHER_API_KEY` | exterior scheduling; blank means conditions are UNKNOWN |

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
