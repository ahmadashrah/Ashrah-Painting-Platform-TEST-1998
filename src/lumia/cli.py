"""Command-line interface.

    lumia status
    lumia seed
    lumia pipeline
    lumia run "find property managers in Calgary with upcoming turnovers"
    lumia daily --focus "general contractors"
    lumia weekly --days 7
    lumia approvals list
    lumia approvals approve appr_abc123 --note "looks good" --execute
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .agents import ROLES
from .llm import MissingAPIKey
from .orchestrator import Orchestrator
from .reporting import growth_review
from .seed import seed_demo_data
from .tools import Toolbox
from .workspace import Workspace


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _report_run(run: Any) -> None:
    print(f"\n[{run.role}] {run.summary()} — stopped: {run.stopped_because}\n")
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

    run_cmd = sub.add_parser("run", help="give Lumia a task")
    run_cmd.add_argument("task", help="what you want done")
    run_cmd.add_argument("--role", choices=ROLES, help="force a specialist instead of auto-routing")

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

    args = parser.parse_args(argv)
    ws = Workspace.build()

    if args.command == "status":
        _print(ws.status())
        return 0

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
        result = Toolbox(ws).call(record["tool"], record["arguments"])
        ws.approvals.mark_executed(record["id"], result if isinstance(result, dict) else {"result": result})
        print("\nExecuted:")
        _print(result)
    elif approved:
        print("\nApproved but not executed. Re-run with --execute to perform it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
