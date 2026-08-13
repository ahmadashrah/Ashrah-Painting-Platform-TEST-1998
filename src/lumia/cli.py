"""Command-line interface.

Growth:

    lumia status
    lumia seed
    lumia pipeline
    lumia run "find property managers in Calgary with upcoming turnovers"
    lumia daily --focus "general contractors"
    lumia weekly --days 7
    lumia approvals list
    lumia approvals approve appr_abc123 --note "looks good" --execute

Project communication:

    lumia comms seed
    lumia comms projects
    lumia comms intake --project proj_abc123
    lumia comms daily-log --project proj_abc123
    lumia comms dispatch
    lumia comms followups
    lumia comms review --days 7 --metrics-only
    lumia comms run "the super wants to know about tomorrow's access"
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .agents import COMMS_ROLES, GROWTH_ROLES
from .comms.desk import CommunicationDesk
from .comms.reporting import communication_review
from .comms.seed import seed_demo_projects
from .facade import Lumia
from .llm import MissingAPIKey
from .orchestrator import Orchestrator
from .reporting import growth_review
from .seed import seed_demo_data
from .tools import Toolbox
from .workspace import Workspace


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _report_run(run: Any) -> None:
    reference = getattr(run, "reference", "") or "unnumbered"
    print(f"\n{reference}  [{run.role}] {run.summary()} — stopped: {run.stopped_because}")
    if getattr(run, "timed_out", False):
        print(f"  hit the {getattr(run, 'budget_seconds', 0):.0f}s limit — "
              "anything not listed below did not happen")
    print()
    for call in run.tool_calls:
        mark = "OK " if call.executed else "GATE"
        print(f"  {mark} L{call.level} {call.tool}: {call.result_summary}")
    if run.approvals_raised:
        print(f"\n  {len(run.approvals_raised)} action(s) queued for approval: "
              f"{', '.join(run.approvals_raised)}")
    print(f"\n{run.reply}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumia", description="Lumia B2B growth agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show model and integration status")
    sub.add_parser("seed", help="load demo accounts so the pipeline is explorable")
    sub.add_parser("pipeline", help="print the pipeline rollup")

    agents_cmd = sub.add_parser("agents", help="every agent, what it does and what it may do")
    agents_cmd.add_argument("--family", choices=["growth", "communication"], default="")
    agents_cmd.add_argument("--role", default="", help="show one role's tools in detail")

    screen_cmd = sub.add_parser("screen", help="check wording against the approval screen")
    screen_cmd.add_argument("text", help="the message text")
    screen_cmd.add_argument("--to", dest="recipient_role", default="client", help="recipient role")
    screen_cmd.add_argument("--subject", default="")

    gate_cmd = sub.add_parser("gate", help="dry-run the autonomy gate for one tool call")
    gate_cmd.add_argument("tool", help="tool name")
    gate_cmd.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE",
                          help="tool argument, repeatable")

    watch_cmd = sub.add_parser("watch", help="live operator window — every step as it happens")
    watch_cmd.add_argument("reference", nargs="?", default="", help="follow one run only")
    watch_cmd.add_argument("--replay", type=int, default=20, help="steps of history to show first")
    watch_cmd.add_argument("--once", action="store_true", help="print what has happened and exit")

    kill_cmd = sub.add_parser("kill", help="stop a run that is already going")
    kill_cmd.add_argument("reference", help="a run reference, or ALL to stop everything")
    kill_cmd.add_argument("--reason", default="", help="why — recorded and shown in the run's reply")
    kill_cmd.add_argument("--release", action="store_true", help="cancel a kill request instead")

    steps_cmd = sub.add_parser("steps", help="every step of one run")
    steps_cmd.add_argument("reference", help="a run reference")

    runs_cmd = sub.add_parser("runs", help="numbered run history, newest first")
    runs_cmd.add_argument("reference", nargs="?", default="",
                          help="a run number or reference (42, RUN-000042) to trace in full")
    runs_cmd.add_argument("--role", default="", help="filter by agent")
    runs_cmd.add_argument("--limit", type=int, default=20)

    run_cmd = sub.add_parser("run", help="give Lumia a task")
    run_cmd.add_argument("task", help="what you want done")
    run_cmd.add_argument("--role", choices=GROWTH_ROLES, help="force a specialist instead of auto-routing")

    daily = sub.add_parser("daily", help="run the daily operating cycle")
    daily.add_argument("--focus", default="", help="optional focus for today")

    weekly = sub.add_parser("weekly", help="produce the weekly growth review")
    weekly.add_argument("--days", type=int, default=7)
    weekly.add_argument("--metrics-only", action="store_true", help="print metrics without calling the model")

    approvals = sub.add_parser("approvals", help="review the Level 3 approval queue")
    approvals.add_argument("action", choices=["list", "approve", "reject"])
    approvals.add_argument("request_id", nargs="?", default="")
    approvals.add_argument("--note", default="")
    approvals.add_argument("--execute", action="store_true", help="run the action immediately on approval")

    _add_comms_commands(sub)

    args = parser.parse_args(argv)
    ws = Workspace.build()

    if args.command == "comms":
        return _comms(ws, args)

    if args.command == "status":
        _print(Lumia(settings=ws.settings).status())
        return 0

    if args.command in {"agents", "screen", "gate", "runs", "watch", "kill", "steps"}:
        return _facade(ws, args)

    if args.command == "seed":
        _print(seed_demo_data(ws))
        return 0

    if args.command == "pipeline":
        _print(ws.crm.pipeline())
        return 0

    if args.command == "approvals":
        return _approvals(ws, args)

    if args.command == "weekly" and args.metrics_only:
        _print(growth_review(ws, days=args.days))
        return 0

    try:
        orchestrator = Orchestrator.build(ws)
        if args.command == "run":
            run = orchestrator.handle(args.task, role=args.role)
        elif args.command == "daily":
            run = orchestrator.daily_loop(focus=args.focus)
        else:
            run = orchestrator.weekly_review(days=args.days)
    except MissingAPIKey as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _report_run(run)
    return 0


def _facade(ws: Workspace, args: argparse.Namespace) -> int:
    """Introspection and dry-runs — none of these need an API key."""
    lumia = Lumia(settings=ws.settings)

    if args.command == "agents":
        if args.role:
            _print(lumia.tools(args.role))
            return 0
        from .phases import describe

        for agent in lumia.agents(family=args.family):
            print(f"\n{agent.role}  [{agent.family}]")
            print(f"  {agent.purpose}")
            phases = describe(agent.role)
            if phases:
                print("  phases: " + " → ".join(p["name"] for p in phases))
            print(f"  {len(agent.tools)} tools — "
                  f"L1 {len(agent.autonomous)} free, "
                  f"L2 {len(agent.controlled)} within rules, "
                  f"L3 {len(agent.approval_required)} need a human")
            if agent.approval_required:
                print(f"  needs approval: {', '.join(agent.approval_required)}")
        return 0

    if args.command == "kill":
        switch = lumia.runner.kill_switch
        if args.release:
            switch.release_all()
            print("All kill requests cleared.")
            return 0
        _print(switch.request(args.reference, reason=args.reason or "stopped by operator"))
        return 0

    if args.command == "steps":
        events = lumia.runner.steps(args.reference)
        if not events:
            print(f"No steps recorded for {args.reference}.")
            return 0
        for event in events:
            print(_step_line(event))
        return 0

    if args.command == "watch":
        return _watch(lumia, args)

    if args.command == "runs":
        if args.reference:
            trace = lumia.runner.trace(args.reference)
            if "error" in trace:
                print(f"error: {trace['error']}", file=sys.stderr)
                return 2
            _print(trace)
            return 0
        history = lumia.runs(limit=args.limit, role=args.role)
        if not history:
            print("No runs recorded yet.")
            return 0
        for record in history:
            held = len(record.get("tools_gated") or [])
            print(
                f"{record.get('reference', '?'):<12} {record.get('role', ''):<14}"
                f" {record.get('status', ''):<9} {record.get('started_at', '')[:19]}"
                f"  {len(record.get('tools_executed') or [])} action(s)"
                + (f", {held} held" if held else "")
            )
            print(f"{'':<12} {str(record.get('task', ''))[:90]}")
            done = [p["phase"] for p in record.get("phases") or []]
            if done:
                print(f"{'':<12} phases: {' → '.join(done)}")
        return 0

    if args.command == "screen":
        result = lumia.screen(args.text, recipient_role=args.recipient_role, subject=args.subject)
        _print(result)
        return 1 if result["requires_approval"] else 0

    # gate
    arguments: dict[str, Any] = {}
    for pair in args.arg:
        key, _, value = pair.partition("=")
        arguments[key.strip()] = value.strip()
    decision = lumia.gate(args.tool, **arguments)
    _print(decision)
    return 1 if decision["level"] == 3 else 0


def _add_comms_commands(sub: Any) -> None:
    """The project-communication side of the CLI."""
    comms = sub.add_parser("comms", help="project communication and reporting")
    actions = comms.add_subparsers(dest="comms_command", required=True)

    actions.add_parser("seed", help="load a demo project with an imperfect day on it")
    actions.add_parser("projects", help="list projects and today's communication state")
    actions.add_parser("followups", help="chase unanswered questions and failed deliveries")

    intake = actions.add_parser("intake", help="process today's field submissions")
    intake.add_argument("--project", default="", help="limit to one proj_... id")
    intake.add_argument("--date", default="", help="ISO work date, defaults to today")

    log = actions.add_parser("daily-log", help="compose and send the client daily log")
    log.add_argument("--project", default="", help="limit to one proj_... id")
    log.add_argument("--date", default="", help="ISO log date, defaults to today")

    dispatch = actions.add_parser("dispatch", help="send the crew dispatch")
    dispatch.add_argument("--project", default="", help="limit to one proj_... id")
    dispatch.add_argument("--date", default="", help="ISO work date, defaults to today")

    review = actions.add_parser("review", help="communication performance review")
    review.add_argument("--days", type=int, default=7)
    review.add_argument("--metrics-only", action="store_true", help="print metrics without calling the model")

    run_cmd = actions.add_parser("run", help="give the communication agent a task")
    run_cmd.add_argument("task", help="what you want done")
    run_cmd.add_argument("--role", choices=COMMS_ROLES, help="force a specialist instead of auto-routing")


def _comms(ws: Workspace, args: argparse.Namespace) -> int:
    command = args.comms_command

    if command == "seed":
        _print(seed_demo_projects(ws))
        return 0

    if command == "projects":
        _print(Toolbox(ws).call("list_projects", {}))
        return 0

    if command == "review" and args.metrics_only:
        _print(communication_review(ws, days=args.days))
        return 0

    try:
        desk = CommunicationDesk.build(ws)
        if command == "intake":
            run = desk.intake(project_id=args.project, work_date=args.date)
        elif command == "daily-log":
            run = desk.daily_logs(project_id=args.project, log_date=args.date)
        elif command == "dispatch":
            run = desk.dispatch(project_id=args.project, work_date=args.date)
        elif command == "followups":
            run = desk.followups()
        elif command == "review":
            run = desk.review(days=args.days)
        else:
            run = desk.handle(args.task, role=args.role)
    except MissingAPIKey as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _report_run(run)
    return 0


def _step_line(event: dict[str, Any]) -> str:
    detail = " ".join(
        f"{k}={v}" for k, v in (event.get("detail") or {}).items()
        if v not in (None, "", [], {}, "None")
    )
    return (
        f"{str(event.get('at', ''))[11:23]:<13}"
        f"{event.get('run', ''):<12} "
        f"{event.get('kind', ''):<16} {detail}"
    )[:200]


def _watch(lumia: Lumia, args: argparse.Namespace) -> int:
    """Tail the step log. The operator window, in a terminal."""
    import time

    recorder = lumia.runner.recorder()
    seen = 0
    history = recorder.read(run=args.reference, limit=args.replay)
    for event in history:
        print(_step_line(event))
    seen = len(recorder.read(run=args.reference, limit=100_000))

    if args.once:
        return 0

    target = args.reference or "every run"
    print(f"\n— watching {target}. Ctrl-C to stop —\n", file=sys.stderr)
    try:
        while True:
            events = recorder.read(run=args.reference, limit=100_000)
            for event in events[seen:]:
                print(_step_line(event), flush=True)
            seen = max(seen, len(events))
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n— stopped watching —", file=sys.stderr)
    return 0


def _approvals(ws: Workspace, args: argparse.Namespace) -> int:
    if args.action == "list":
        pending = ws.approvals.pending()
        if not pending:
            print("No actions awaiting approval.")
            return 0
        for request in pending:
            print(f"\n{request['id']}  [{request.get('agent', '?')}]  {request['tool']}")
            print(f"  reason: {request['reason']}")
            print(f"  args:   {json.dumps(request['arguments'], default=str)[:400]}")
        return 0

    if not args.request_id:
        print("error: an approval id is required", file=sys.stderr)
        return 2

    approved = args.action == "approve"
    record = ws.approvals.decide(args.request_id, approved=approved, note=args.note)
    if "error" in record:
        print(f"error: {record['error']}", file=sys.stderr)
        return 2

    _print(record)

    if approved and args.execute:
        # Pass the approval id through so the action records who cleared it.
        arguments = {**record["arguments"], "approval_id": record["id"]}
        result = Toolbox(ws).call(record["tool"], arguments)
        ws.approvals.mark_executed(record["id"], result if isinstance(result, dict) else {"result": result})
        print("\nExecuted:")
        _print(result)
    elif approved:
        print("\nApproved but not executed. Re-run with --execute to perform it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
