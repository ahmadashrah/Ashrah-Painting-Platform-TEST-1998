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

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .agents import COMMS_ROLES, ROLE_TOOLS
from .autonomy import AUTO_SENDABLE_KINDS, TOOL_LEVELS, AutonomyLevel, classify
from .comms.screening import screen
from .domain.projects import CommKind, RecipientRole
from .facade import PURPOSE

log = logging.getLogger("lumia.server")

PAGE = Path(__file__).resolve().parents[2] / "docs" / "index.html"

#: Requests larger than this are refused rather than read into memory.
MAX_BODY_BYTES = 64 * 1024


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
            if not PAGE.is_file():
                return self._send(500, {"error": f"page not found at {PAGE}"})
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")

        if path == "/api/health":
            return self._send(200, {
                "ok": True,
                "engine": "python",
                "agents": len(ROLE_TOOLS),
                "routine_kinds": sorted(AUTO_SENDABLE_KINDS),
                "kinds": [k.value for k in CommKind],
                "roles": [r.value for r in RecipientRole],
            })

        if path == "/api/agents":
            return self._send(200, {"agents": agent_roster()})

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

        return self._send(404, {"error": f"no route {path}"})


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Railway and most hosts inject PORT; bind all interfaces so the
    # platform's router can reach the container.
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("Lumia control room on http://%s:%s  (%d agents)", host, port, len(ROLE_TOOLS))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
