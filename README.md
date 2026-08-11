# Lumia

**The B2B marketing and growth agent for Ashrah Painting Ltd.**

Lumia is not an email writer or a lead-list generator. It is a growth
operator: a system that turns market intelligence into target accounts,
accounts into relationships, relationships into estimates, and estimates
into revenue — recording what worked at every step so the next cycle is
better informed than the last.

It runs on Claude Opus 5 with a harness that enforces the parts you cannot
leave to a prompt: autonomy levels, an approval gate, deterministic
scoring, and a memory that accumulates evidence.

```bash
git clone https://github.com/ahmadashrah/ashrah-painting-platform-test-1998.git
cd ashrah-painting-platform-test-1998
pip install -r requirements-dev.txt
cp .env.example .env          # add your ANTHROPIC_API_KEY

export PYTHONPATH=src
python -m lumia.cli status    # what's live vs mocked
python -m lumia.cli seed      # load demo accounts
python -m lumia.cli daily     # run today's operating cycle
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

## Autonomy levels are enforced in code

The spec defines three levels. This repo makes them real: every tool is
classified, and the agent loop physically cannot execute a Level 3 call.

| Level | Examples | Behaviour |
|---|---|---|
| **1 — Autonomous** | research, scoring, drafting, reporting, CRM reads | runs freely |
| **2 — Controlled** | CRM writes, routine follow-ups, scheduling | runs within approved rules |
| **3 — Approval required** | first contact, publishing, pricing commitments | **queued for a human** |

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

---

## Architecture

```
src/lumia/
├── prompts.py        Lumia's operating spec, versioned and composable
├── agent.py          the tool loop — where the autonomy gate intercepts
├── autonomy.py       Level 1/2/3 classification + approval queue
├── orchestrator.py   routing, the daily cycle, the weekly review
├── tools.py          every capability the agent has
├── memory.py         ARE ledger + Marketing Learning Memory
├── reporting.py      weekly metrics (computed, never narrated)
├── workspace.py      wiring: config → store → integrations
├── agents/           role prompts and per-role tool allowlists
├── domain/
│   ├── accounts.py   accounts, contacts, interactions, opportunities, signals
│   ├── scoring.py    the six-factor lead score and tiering
│   └── pricing.py    the estimating rate card
└── integrations/     CRM, email, SMS, calendar, search, permits, weather
```

**Specialists.** Five roles share the core prompt and get a restricted
toolset: `director`, `research`, `outreach`, `content`, `crm`. Requests are
auto-routed; the Content agent cannot email a prospect because it does not
hold the tool.

```bash
python -m lumia.cli run "find property managers in Calgary with turnovers coming"
python -m lumia.cli run "draft a case study from the Riverbend job" --role content
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
4. Add a tool in `tools.py` with a description that says **when** to call
   it, and classify it in `autonomy.py`.

Step 4 is not optional: a tool missing from the autonomy table is treated
as Level 3 and will be gated on every call.

---

## Development

```bash
python -m pytest -q          # 53 tests, no network, no API key needed
```

The suite drives the agent loop with a scripted fake client, so the gate,
the CRM round trip, scoring, pricing and the reporting maths are all
covered without spending a token.

**Demo data.** `lumia seed` writes three fictional accounts tagged
`source: "demo_seed"` and named `(DEMO)`. Delete `data/` before pointing
this at real accounts.

**State.** All local state lives in `data/` (git-ignored): `lumia.json`
holds the CRM and memory, `approvals.json` the pending gate queue. Point
`ASHRAH_DATA_DIR` elsewhere to isolate environments.

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
