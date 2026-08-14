"""The control room, served by the engine it describes.

The standalone page in `docs/` carries a JavaScript port of the screening
rules so it works from a file:// URL. A port is a second implementation,
and second implementations drift. When this server is running, the page
stops using its own copy and asks the real one instead — the same
`screen()` and `classify()` the agents go through.

Deliberately read-only and stateless. Anything hosted is reachable by
whoever has the URL, so the API exposes no project, client, contact or
account data, and nothing here can send a message, write a record or spend
a token. It answers two questions — *would this message need a human*, and
*what may each agent do* — and both are computed from the request.

Standard library only. This is an internal control panel, not a public
service, and a zero-dependency server is one less thing to keep current.

    PYTHONPATH=src python -m lumia.server          # http://localhost:8000
    PORT=3000 PYTHONPATH=src python -m lumia.server
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .agents import COMMS_ROLES, ROLE_TOOLS
from .autonomy import AUTO_SENDABLE_KINDS, TOOL_LEVELS, AutonomyLevel, classify
from .comms.screening import screen
from .config import SETTINGS
from .domain.projects import CommKind, RecipientRole
from .facade import PURPOSE
from .runner import Runner

log = logging.getLogger("lumia.server")

def _find_page() -> Path | None:
    """Locate the control room page, wherever this is deployed from.

    `parents[2]/docs` is right for a repo checkout and wrong for an
    installed package, where it resolves into site-packages. A deploy that
    pip-installs the project then serves a 500 on `/` while the health check
    still returns 200 — up by every automated measure, broken to anyone who
    opens it.
    """
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "docs" / "index.html",   # repo checkout: src/lumia/..
        Path.cwd() / "docs" / "index.html",        # deployed working directory
        here.parent / "docs" / "index.html",       # shipped as package data
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


PAGE = _find_page()

#: Requests larger than this are refused rather than read into memory.
MAX_BODY_BYTES = 64 * 1024

#: Running an agent spends tokens and can send real messages, so it is off
#: unless LUMIA_RUN_TOKEN is set and the caller presents it. A hosted URL is
#: reachable by anyone who has it; "off by default" is the only safe default.
RUN_TOKEN = os.environ.get("LUMIA_RUN_TOKEN", "")

#: Web runs are capped below the CLI's limit — a browser request that takes
#: twelve model turns has usually timed out at some proxy before it returns.
WEB_MAX_ITERATIONS = 8


def runs_enabled() -> bool:
    return bool(RUN_TOKEN and SETTINGS.anthropic_api_key)


def _token_ok(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> bool:
    supplied = str(handler.headers.get("X-Lumia-Token") or payload.get("token") or "")
    return bool(supplied) and hmac.compare_digest(supplied, RUN_TOKEN)


def agent_roster() -> list[dict[str, Any]]:
    """Every agent and what each of its tools costs it, from the real tables."""
    roster = []
    for role, tools in ROLE_TOOLS.items():
        levels: dict[int, list[str]] = {1: [], 2: [], 3: []}
        for tool in tools:
            levels[int(TOOL_LEVELS.get(tool, AutonomyLevel.APPROVAL_REQUIRED))].append(tool)
        roster.append(
            {
                "role": role,
                "family": "communication" if role in COMMS_ROLES else "growth",
                "purpose": PURPOSE.get(role, ""),
                "tools": len(tools),
                "l1": len(levels[1]),
                "l2": len(levels[2]),
                "l3": len(levels[3]),
                "needs_approval": sorted(levels[3]),
            }
        )
    return sorted(roster, key=lambda a: (a["family"], a["role"]))


def gate_verdict(kind: str, recipient_role: str, subject: str, body: str) -> dict[str, Any]:
    """Run the real send gate against a message that is never stored.

    `classify` reads a communication record, so one is built here and
    thrown away. Same function, same order of checks, no side effects.
    """
    draft = {
        "id": "comm_preview",
        "kind": kind,
        "recipient_role": recipient_role,
        "recipient_name": "the recipient",
        "recipient_verified": True,
        "subject": subject,
        "body": body,
    }
    level, reason = classify("send_communication", {"draft_id": "comm_preview"}, None, draft)
    result = screen(body, recipient_role=recipient_role, subject=subject)
    return {
        "level": int(level),
        "held": level is AutonomyLevel.APPROVAL_REQUIRED,
        "reason": reason,
        "kind_is_routine": kind in AUTO_SENDABLE_KINDS,
        "screening": result.to_dict(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "lumia"

    # --- plumbing -------------------------------------------------------

    def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page and the API are same-origin; nothing here is for third parties.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError(f"request body is larger than {MAX_BODY_BYTES} bytes")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError as exc:
            raise ValueError(f"body is not valid JSON: {exc}") from exc

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), fmt % args)

    # --- routes ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path == "/":
            # Re-resolved per request: the page may be findable now even if it
            # was not at import time, and a stale miss should not be permanent.
            page = PAGE or _find_page()
            if page is None:
                return self._send(500, {
                    "error": "the control room page was not found",
                    "looked_in": [str(Path.cwd() / "docs"), str(Path(__file__).resolve().parents[2] / "docs")],
                    "fix": "deploy with docs/index.html present, or run from the repository root",
                })
            return self._send(200, page.read_bytes(), "text/html; charset=utf-8")

        if path == "/api/health":
            return self._send(200, {
                "ok": True,
                "engine": "python",
                "agents": len(ROLE_TOOLS),
                "routine_kinds": sorted(AUTO_SENDABLE_KINDS),
                "kinds": [k.value for k in CommKind],
                "roles": [r.value for r in RecipientRole],
                "runs_enabled": runs_enabled(),
                "model_available": bool(SETTINGS.anthropic_api_key),
            })

        if path == "/api/agents":
            return self._send(200, {"agents": agent_roster()})

        if path.startswith("/api/steps"):
            # Steps name tools, levels and reasons — operational detail, not
            # client data — but they are still gated behind the run token so a
            # public URL does not narrate the company's work to strangers.
            if not _token_ok(self, {}):
                return self._send(403, {"error": "wrong or missing token"})
            reference = path.rsplit("/", 1)[-1]
            runner = Runner()
            if reference in ("steps", ""):
                return self._send(200, {"runs": runner.history(limit=25)})
            return self._send(200, {
                "run": runner.get(reference),
                "steps": runner.steps(reference),
            })

        return self._send(404, {"error": f"no route {path}"})

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        try:
            payload = self._read_json()
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})

        text = str(payload.get("body") or payload.get("text") or "")
        role = str(payload.get("recipient_role") or RecipientRole.CLIENT.value)
        subject = str(payload.get("subject") or "")

        if path == "/api/screen":
            return self._send(200, screen(text, recipient_role=role, subject=subject).to_dict())

        if path == "/api/gate":
            kind = str(payload.get("kind") or CommKind.OTHER.value)
            return self._send(200, gate_verdict(kind, role, subject, text))

        if path == "/api/run":
            return self._run(payload)

        if path == "/api/kill":
            if not RUN_TOKEN:
                return self._send(503, {"error": "operator control over HTTP is disabled",
                                        "fix": "set LUMIA_RUN_TOKEN to enable it"})
            if not _token_ok(self, payload):
                return self._send(403, {"error": "wrong or missing token"})
            reference = str(payload.get("run") or "").strip()
            if not reference:
                return self._send(400, {"error": "name the run to stop, or ALL"})
            return self._send(200, Runner().kill_switch.request(
                reference, reason=str(payload.get("reason") or "stopped by operator")))

        return self._send(404, {"error": f"no route {path}"})

    def _run(self, payload: dict[str, Any]) -> None:
        """Run one agent, cold. Off unless a token is configured and presented."""
        if not RUN_TOKEN:
            return self._send(503, {
                "error": "running agents over HTTP is disabled",
                "fix": "set LUMIA_RUN_TOKEN in the host's variables to enable it, "
                       "then send that token as X-Lumia-Token.",
            })
        if not SETTINGS.anthropic_api_key:
            return self._send(503, {
                "error": "ANTHROPIC_API_KEY is not set, so agents cannot run",
                "fix": "add it to the host's variables. Screening and the gate work without it.",
            })
        if not _token_ok(self, payload):
            return self._send(403, {"error": "wrong or missing token"})

        role = str(payload.get("role") or "")
        task = str(payload.get("task") or "").strip()
        if role not in ROLE_TOOLS:
            return self._send(400, {"error": f"unknown agent '{role}'"})
        if not task:
            return self._send(400, {"error": "say what you want the agent to do"})

        record = Runner().run(role, task, max_iterations=WEB_MAX_ITERATIONS)
        return self._send(200, {
            "id": record.id,
            "run": record.reference,
            "number": record.number,
            "role": record.role,
            "status": record.status,
            "reply": record.reply,
            "iterations": record.iterations,
            "did": record.tools_executed,
            "held_for_approval": record.tools_gated,
            "approvals": record.approvals_raised,
            "seconds": record.duration_seconds,
            "error": record.error,
            "isolated": record.isolated,
        })


def startup_report() -> dict[str, Any]:
    """What this deployment actually is, printed where an operator will see it.

    A container that boots and then behaves unexpectedly is usually
    misconfigured rather than broken, and the answer is almost always in
    these six lines. Printing them costs nothing and saves reading the
    source to find out whether a key was set.
    """
    from datetime import date, datetime

    from .contract import current_contract

    explicit_data_dir = bool(os.environ.get("ASHRAH_DATA_DIR"))
    return {
        "node": os.environ.get("LUMIA_NODE", "") or "hostname",
        "agents": len(ROLE_TOOLS),
        "model": SETTINGS.model,
        "model_key_set": bool(SETTINGS.anthropic_api_key),
        "runs_over_http": runs_enabled(),
        "contract": current_contract(SETTINGS).fingerprint(),
        "data_dir": str(SETTINGS.data_dir),
        "data_is_persistent": explicit_data_dir,
        "today": date.today().isoformat(),
        "clock": datetime.now().astimezone().tzname() or "UTC",
        "page_found": _find_page() is not None,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    # Railway and most hosts inject PORT; bind all interfaces so the
    # platform's router can reach the container.
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True

    report = startup_report()
    log.info("Lumia control room on http://%s:%s", host, port)
    for key, value in report.items():
        log.info("  %-18s %s", key, value)

    if not report["data_is_persistent"]:
        log.warning(
            "  NOTE  ASHRAH_DATA_DIR is not set, so %s lives in the container and is "
            "erased on every redeploy — projects, sent messages, approvals and run "
            "numbers with it. Attach a volume and point ASHRAH_DATA_DIR at it.",
            report["data_dir"],
        )
    if not report["page_found"]:
        log.warning("  NOTE  docs/index.html was not found; the control room page will 500.")

    # Railway sends SIGTERM on every redeploy and scale-down. Without a
    # handler the process is killed outright partway through whatever it was
    # doing; with one it stops accepting work and closes the socket cleanly.
    def shutdown(signum: int, _frame: Any) -> None:
        log.info("received %s — shutting down", signal.Signals(signum).name)
        threading.Thread(target=server.shutdown, daemon=True).start()

    for received in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(received, shutdown)
        except ValueError:  # pragma: no cover - not the main thread
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        server.server_close()
    log.info("stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
