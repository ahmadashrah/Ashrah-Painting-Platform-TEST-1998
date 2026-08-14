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

Both run on Claude Opus 5 — or on GPT, by changing one variable — with a
harness that enforces the parts you cannot leave to a prompt: autonomy
levels, an approval gate, deterministic scoring and screening, and a memory
that accumulates evidence.

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

## OpenAI, where it is the better tool

Three uses, none of which replaces the harness around them.

**Whisper transcribes what the field actually sends.** Crews report by voice
in Arabic, Kurdish and French. `transcribe_field_submission` fills an empty
transcript once and never rewrites one — a recording says what it says, and
the chain from audio to sent message has to stay intact. Without the key it
*refuses*, and tells the agent to ask for text rather than writing a
transcript from what it expects the recording to say.

**Vision reads a photo; it does not caption one.** `describe_media` reports
what is visible and flags likely problems — faces, documents, keypads, missing
PPE — then stops. The caption a client reads is a statement Ashrah is making,
so it stays a reviewed step through `caption_media`, which still refuses any
completion claim.

**Embeddings give memory recall by meaning.** "Site radio silence from the
contractor" now surfaces the lesson about a GC who stopped replying to long
emails, which shares no keyword with it. Lessons stored before embeddings were
switched on are backfilled on first use, so enabling this cannot make the
oldest and best-evidenced lessons invisible. With no key it falls back to
keyword overlap — a memory that went silent when a key was missing is a
memory nobody would notice losing.

**The agents can also run on GPT.** Set `ASHRAH_MODEL` to a GPT model and
they do; the provider follows the model name, so there is no second switch to
forget.

```bash
ASHRAH_MODEL=gpt-4o python -m lumia.cli comms intake
```

Translation happens at the client boundary — `OpenAIClient` accepts the same
call `ClaudeClient` does and returns the same block shape, converting tool
calls and results in both directions. The loop, the autonomy gate, the
approval queue and the content screen are then byte-identical on either
provider, which is the point: the safety machinery must not have two
implementations, one of which is less tested. A test drives a GPT-shaped
response through the real loop and asserts a priced daily log still lands in
the approval queue.

---

## Runs happen in phases

A run used to be one long conversation holding every tool its role owned,
from the first read to the last send. Now it moves through named phases, and
each phase holds only the tools it needs.

```
1. gather     9 tools available | used: build_daily_log, list_field_submissions
2. compose    3 tools available | used: compose_daily_log
3. review     4 tools available | used: recommend_channel
4. send       4 tools available | used: draft_communication, send_communication
5. record     7 tools available | used: raise_open_item
```

Two things this buys.

**It is watchable.** "The client agent did eleven things" says almost
nothing; "it gathered, composed, reviewed, then sent" says where the run is
and what should happen next. Every phase boundary is an event, and each
phase's goal, available tools, tools used and result are recorded on the run.

**A phase cannot reach past itself.** An agent gathering facts does not hold
`send_communication`; one composing a log cannot order paint. A test asserts
no role's first phase holds any sending tool at all.

That restriction is enforced **at dispatch**, not by omitting the schema.
Leaving a tool out of the list sent to the model makes it unlikely to be
called; checking before running makes it impossible:

```
'send_communication' is not available in the gather phase.
Available here: build_daily_log, communication_history, get_project, …
```

The refusal names what *is* available, because a refusal that does not just
gets retried. This closed a real gap: `allowed_tools` had only ever filtered
the schema list, and nothing checked it at dispatch.

A phase hands the next its written result, not its transcript — the composer
receives the gathered facts as findings. Phases share one run number, one
kill switch and, where one is set, one time budget: five phases never mean
five times the budget.

Adding a phase is adding an entry to `phases.py`. Its tools are intersected
with the role's own allowlist, so a plan can narrow a role and never widen
it. A role with no plan runs as a single phase holding its whole toolset —
new agents are unphased, never unable to run.

---

## Stepping outside the contract kills the run

A run may use the tools its phase holds and call the services this
deployment is configured for. Reaching outside that is not a wrong guess to
be corrected mid-flight — it is the run doing something nobody authorized,
and it stops immediately.

**What counts as a breach**

| | |
|---|---|
| a tool the current phase does not hold | `tool_out_of_phase` |
| a tool that does not exist, or the role never held | `unknown_tool` |
| an HTTP call to a host this deployment is not configured for | `out_of_scope_host` |

```
RUN-000001 status=killed breached=True
Stopped: 'send_communication' is not available in the gather phase.
Available here: build_daily_log, communication_history, get_project, …
```

**What deliberately does not count.** Being *gated* is not a breach. A Level
3 tool queued for approval is the harness working exactly as designed, and
killing the run for it would punish the agent for using the approval path
correctly — a test pins that a gated material order finishes cleanly with
`breached: False`. A tool that runs and returns an error is not a breach
either; it was reached legitimately and the agent should recover.

**The network side is the one that mattered most.** The agent supplies URLs
— a photo's location, a recording to transcribe — and those get fetched.
Without a check, "transcribe this audio" is a request to fetch *any* address
the model can name from inside your network, including `169.254.169.254`.
Every outbound call is now checked against the hosts this deployment is
configured for: the base URL of each service with credentials, the model
providers, and anything named in `LUMIA_ALLOWED_HOSTS`. A subdomain of an
allowed host passes; `evil-example.com` does not pass for `example.com`.

**Why killing rather than refusing.** Refusing tells the agent "not that
one" and lets it try again, which is right for a wrong guess and wrong for
an attempt to act outside scope. Once a run has stepped outside its
contract, nothing it does afterwards can be assumed to be inside it, and the
cheapest safe state is stopped.

The trade is real: a model reaching early for a tool it would hold in a
later phase now ends the run rather than being corrected. Set
`LUMIA_ON_BREACH=refuse` where that trade is not wanted.

Every breach is recorded on the run — what was attempted, in which phase,
and what was available instead — and closes the step log as `run.killed`,
never as a clean finish.

---

## L5: many runtimes, one fabric

One runtime enforcing its own contract is enough while everything runs in
one process. It stops being enough the moment there is a scheduler running
the daily-log cycle, an operator running intake from a laptop, and the web
console serving a phone — three runtimes, no shared view, no way to stop
them together.

```bash
python -m lumia.cli fleet status      # what is running, where, in which phase
python -m lumia.cli fleet contract    # do this node's rules match the fleet's
python -m lumia.cli fleet publish     # make this node's rules the fleet's
python -m lumia.cli fleet pause       # hold every run at its next step
python -m lumia.cli fleet resume
python -m lumia.cli fleet sweep       # drop entries whose runtime died
```

**1. Shared runtime registry.** Every active run, with its node, its role
and the phase it is in right now. Entries are written per-run rather than
into one index, so two runtimes starting at once never contend and a
crashed process leaves one stale entry rather than a corrupt file. Entries
go stale after 90 seconds — a dead runtime stops blocking the fleet instead
of holding it forever.

**2. Distributed lifecycle commands.** `terminate` was already there as the
kill switch. `pause` and `resume` join it: a paused run *holds* — keeping
its number, its registry entry and its phase — and continues where it left
off. It stays killable while held, because an operator who pauses, looks,
and decides to stop should not have to resume first.

**3. Consistent contract distribution.** The contract — every tool's level,
the auto-sendable kinds, every phase plan, the allowed hosts, the breach
policy — is built *from the live tables*, not written down beside them, and
versioned by a fingerprint of the rules themselves:

```
Published contract lumia@9271823efe7a.
Runtimes enforcing different rules will now refuse to run.
```

Because the version is derived, two nodes agreeing on it means their rules
genuinely match. A node on a stale deploy produces a different fingerprint
and **refuses to start**, which is the entire reason to distribute a
contract rather than assume one.

**4. Cross-runtime containment.** Runtimes may not call each other by
default. An agent that can ask a peer on another host to act for it has no
containment at all. Nodes coordinate through the shared registry and bus;
`LUMIA_APPROVED_RUNTIMES` opens a route deliberately.

**5. Coordinated phase transitions.** Give runs a cohort and no one starts a
phase until every peer has reached it — nothing sends while another agent is
still gathering the report it depends on. The barrier always times out
rather than stalling: one wedged runtime must not stop everyone, so a run
proceeds alone and records who it left behind.

**What backs this.** The shared data directory with file locks — the same
substrate the run counter uses. That genuinely coordinates every process on
one host, which is this deployment. It is not multi-region, and the module
says so: the registry, bus and contract store are narrow interfaces over a
path, so real multi-host work means swapping the storage inside those three
classes and nothing above them. A fleet that reported consistency it did not
have would be worse than one that admits it is a single host.

---

## The operator's window

Every step a run takes is an event, and an operator can watch them live or
read them back afterwards.

```bash
python -m lumia.cli watch                 # live, every run
python -m lumia.cli watch RUN-000042      # live, one run
python -m lumia.cli steps RUN-000042      # what it did, after the fact
```

```
01:36:12.824 RUN-000042  run.started      task=order primer number=42
01:36:12.825 RUN-000042  phase.started    phase=gather index=1 of=3
01:36:12.825 RUN-000042  turn.started     turn=1
01:36:12.825 RUN-000042  model.replied    turn=1 stop_reason=tool_use tool_calls=1
01:36:12.825 RUN-000042  tool.proposed    tool=place_material_order arguments={…}
01:36:12.826 RUN-000042  gate.decided     tool=place_material_order level=3 reason=…
01:36:12.826 RUN-000042  tool.gated       tool=place_material_order approval_id=appr_…
01:36:12.827 RUN-000042  run.finished     actions=1 held=1 timed_out=False killed=False
```

The two lines worth watching are `gate.decided` and `tool.gated`: what the
agent asked to do, what the harness decided, and why.

**Attaching your own hook** takes one call. It sees everything, including
runs started by code you did not write and agents that do not exist yet:

```python
from lumia.observability import HOOKS

HOOKS.subscribe(lambda event: my_dashboard.push(event.to_dict()), name="ops")
```

Two rules protect the run from the hook. A subscriber that raises is logged
and skipped — an operator's broken dashboard cannot take down the crew
dispatch, and a test asserts a later hook still fires after an earlier one
throws. And payloads are trimmed before recording: a tool result can be 60KB,
and the step log is for watching, not for storing.

## Stopping a run

Two mechanisms, because they fail differently.

**The kill switch** is for a run doing the wrong thing. It is file-based, so
it reaches a run in another process — a scheduled cycle, a web request, a
worker:

```bash
python -m lumia.cli kill RUN-000042 --reason "wrong recipient"
python -m lumia.cli kill ALL --reason "stop everything"
python -m lumia.cli kill ALL --release
```

The run stops at its next step and says what had already happened. A
reference is never reused, so a request can only ever mean the run it names —
including one armed before that run starts.

**The watchdog** is for a run that has stopped policing itself. The time
budget is cooperative: it works because the loop checks it. That covers a
slow run, not one blocked *below* the loop — a socket opened without a
timeout, a library that swallowed the one we passed, a retry buried in a
vendor SDK. A timer signal interrupts the blocked call itself, 15 seconds
past the budget, so the clean stop normally wins the race. Measured: a call
that would have blocked for 60 seconds is interrupted after 5.

It needs the main thread of a POSIX process. Under a thread pool it does
nothing, which is exactly why the cooperative checks sit at three points
rather than being left to a watchdog that may not be there.

Both are recorded. A killed or interrupted run keeps its number, its step log
and its place in `lumia runs`.

---

## Time budgets, if you want them

Runs are **uncapped by default**. A 120-second limit was tried and
cancelled: a communication cut off part-way leaves a client half-told and a
crew half-briefed, and an incomplete message costs more than a slow one.
Runs are stopped by an operator, not by a clock — see the kill switch below.

The machinery stays, because a budget is still right for some cases: a web
request that has to return, or a scheduled job that must not overlap the
next one.

```bash
LUMIA_COMMS_BUDGET_SECONDS=120 python -m lumia.cli comms daily-log
```

```python
lumia.runner.run("client_comms", "send the log", budget_seconds=45)
```

When a budget *is* set, it is enforced at three points, because one is not
enough:

| | |
|---|---|
| **Between turns** | the loop will not start another model call on a spent budget |
| **Before a tool call** | no new outbound work begins with no time left |
| **Inside each HTTP call** | every request gets what is actually left, and retries stop when there is no room for another attempt |

That third one matters most. A send with three retries at twenty seconds
each takes 61 seconds unaided, and transcription asks for 90 — under a cap,
both size themselves against what the run has left.

What a budget never does is abort a request already in flight. A send
cancelled mid-write may still have been delivered, and a message the record
shows as unsent but the client received is worse than one that finishes
late. It stops new work; it never interrupts committed work.

A run that does hit its budget stops and says exactly what happened, and is
recorded with `timed_out` set so it is visible in `lumia runs` afterwards.

---

## Every run has a number

A number is issued at creation — before the agent takes its first turn — so a
run that fails immediately is still findable. One sequence covers every agent,
current and future: `RUN-000001` is a run, not an intake run.

```bash
python -m lumia.cli runs                # newest first, across all agents
python -m lumia.cli runs RUN-000042     # everything that run touched
python -m lumia.cli runs --role intake
```

The number reaches what the run produced. `LocalStore` stamps `_run` on every
record written while a run is active, so an escalation, a sent message or an
approval can be traced back to the job that caused it — including from tools
that do not exist yet, because the stamp is in the store rather than in each
tool. Records written outside a run are left unstamped: seeding is not a run
and should not claim to be.

Allocation is done under an exclusive file lock, so two workers cannot take
the same number. A test hands 200 numbers to eight concurrent threads and
asserts they are exactly 1–200; a lost counter file resumes from the highest
run already recorded rather than restarting at one. Skipping a range is
survivable — two runs answering to the same reference would make every record
citing it ambiguous.

The standing cycles go through the same path. `comms intake`, `daily-log`,
`dispatch`, `followups`, `review` and the growth cycles each get their own
number and their own cold, isolated stack; a cycle that skipped numbering
would be exactly the run nobody could trace later.

---

## The control room

A page that drives the send gate live: type a message, pick who it goes to,
and watch it clear or get held.

```bash
PYTHONPATH=src python -m lumia.server     # http://localhost:8000
```

`docs/index.html` is self-contained and opens straight from the filesystem,
carrying a JavaScript port of the screening rules so it works offline. When
the server is running the page detects it and stops using that copy — the
verdict then comes from `screening.py` itself, and it says which engine
answered.

The API is read-only and stateless by design. Anything hosted is reachable
by whoever has the URL, so it exposes no project, client, contact or account
data, and nothing on it can send a message, write a record or spend a token.

| Route | |
|---|---|
| `GET /` | the control room |
| `GET /api/health` | engine, agent count, which message kinds are routine |
| `GET /api/agents` | every agent, its tools and its levels |
| `POST /api/screen` | `{text, recipient_role, subject}` → would this need a human |
| `POST /api/gate` | `{kind, recipient_role, body}` → the real send-gate verdict |

**Deploying it.** The server is standard-library only, so a host needs no
build step beyond installing `requirements.txt`. It binds `PORT` and `HOST`
from the environment, answers `/api/health`, and shuts down on `SIGTERM`
instead of being killed mid-request on every redeploy.

```bash
railway login && railway init && railway up
```

Four files exist because of one deploy that failed for four separate
reasons, each hidden behind the one before it:

| | |
|---|---|
| `railpack.json` | Railway's builder is Railpack, and it does not read `railway.json`'s build config. Without this it reports **"No start command detected"** and never gets as far as running anything. |
| `main.py` | Railpack installs `requirements.txt`, not this project, so `python -m lumia.server` raises `ModuleNotFoundError`. This puts `src` on the path first, and turns stdout line-buffered so logs appear while the container is alive rather than after it dies. |
| `.python-version` | Otherwise the builder picks a Python and the deploy is running a version nobody chose. |
| `Procfile` | For hosts that read one instead. All three name the same command: `python main.py`. |

Two variables are worth setting beyond the API keys:

- **`ASHRAH_DATA_DIR`**, pointed at an attached volume. Without it the
  container's own filesystem holds every project, sent message, approval and
  run number — all of it erased on the next redeploy, including the run
  counter, so numbers restart at 1. The server warns about this at startup
  rather than letting it be discovered later.
- **`TZ`** (`America/Toronto`), because a daily log dated by a container
  running UTC rolls over at 8pm local and files the evening's work under
  tomorrow.

The first thing in the logs is what the deployment actually is — node, agent
count, model, whether its key is set, contract fingerprint, data directory
and whether it persists, today's date and the clock it came from. A container
that boots and then behaves oddly is usually misconfigured, and the answer is
normally in those lines.

Setting `ANTHROPIC_API_KEY` in the host's variables is what makes the agents
able to reason; without it the deployment still serves the control room and
every deterministic rule.

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
python -m pytest -q          # 337 tests, no network, no API key needed
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
| `OPENAI_API_KEY` | Whisper transcription, photo vision, embeddings; also needed if `ASHRAH_MODEL` is a GPT model |
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
