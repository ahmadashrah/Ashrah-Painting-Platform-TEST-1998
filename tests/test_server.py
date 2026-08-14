"""The control room's API. Read-only, stateless, and the real engine."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from lumia.server import Handler, agent_roster, gate_verdict


@pytest.fixture
def base_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def get(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


def post(url: str, payload: dict):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


# --- the engine, not a copy of it ---------------------------------------


def test_a_clean_routine_message_sends():
    verdict = gate_verdict(
        "daily_log",
        "project_manager",
        "Daily Project Update",
        "Surface preparation completed in the north corridor.",
    )
    assert verdict["level"] == 2
    assert verdict["held"] is False


def test_a_price_holds_the_same_message():
    verdict = gate_verdict(
        "daily_log", "project_manager", "Daily Project Update", "The extra work will be $4,200."
    )
    assert verdict["level"] == 3
    assert verdict["held"] is True
    assert "pricing" in verdict["screening"]["categories"]


def test_a_non_routine_kind_is_held_before_its_wording_matters():
    verdict = gate_verdict("commitment", "client", "", "Confirming what we discussed.")
    assert verdict["held"] is True
    assert verdict["kind_is_routine"] is False


def test_the_roster_comes_from_the_real_tables():
    roster = agent_roster()
    assert len(roster) == 10
    vendor = next(a for a in roster if a["role"] == "vendor_comms")
    assert vendor["needs_approval"] == ["place_material_order"]
    for agent in roster:
        assert agent["l1"] + agent["l2"] + agent["l3"] == agent["tools"]


# --- over HTTP ------------------------------------------------------------


def test_health_reports_the_python_engine(base_url):
    status, body = get(f"{base_url}/api/health")
    assert status == 200
    payload = json.loads(body)
    assert payload["engine"] == "python"
    assert payload["agents"] == 10
    assert "daily_log" in payload["routine_kinds"]


def test_the_page_is_served(base_url):
    status, body = get(f"{base_url}/")
    assert status == 200
    assert b"Lumia Control Room" in body


def test_screen_endpoint(base_url):
    status, payload = post(f"{base_url}/api/screen", {"text": "We guarantee completion by Friday."})
    assert status == 200
    assert payload["requires_approval"] is True


def test_gate_endpoint(base_url):
    status, payload = post(
        f"{base_url}/api/gate",
        {"kind": "arrival_notice", "recipient_role": "superintendent", "body": "Crew arriving at 18:00."},
    )
    assert status == 200
    assert payload["level"] == 2


def test_agents_endpoint(base_url):
    status, body = get(f"{base_url}/api/agents")
    assert status == 200
    assert len(json.loads(body)["agents"]) == 10


def test_bad_json_is_a_clear_400(base_url):
    request = urllib.request.Request(
        f"{base_url}/api/screen", data=b"not json", headers={"Content-Type": "application/json"}
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    assert excinfo.value.code == 400
    assert "not valid JSON" in json.loads(excinfo.value.read())["error"]


def test_unknown_routes_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base_url}/api/nope", timeout=5)
    assert excinfo.value.code == 404


def test_no_route_exposes_project_or_client_data(base_url):
    """Anything hosted is reachable by whoever has the URL."""
    _, health = get(f"{base_url}/api/health")
    _, agents = get(f"{base_url}/api/agents")
    combined = (health + agents).decode().lower()
    for leak in ("riverbend", "whitfield", "northgate", "example.com", "+1555"):
        assert leak not in combined


def test_no_route_sends_without_going_through_the_gate(base_url):
    """Sending has exactly one door, and it is the approval queue.

    There is no general "call this tool" route and no direct send: the only
    way a message leaves over HTTP is a Level 3 action a human approved with
    `execute`. Anything that would bypass that must stay a 404.
    """
    for route in ("/api/send", "/api/call", "/api/projects"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(
                urllib.request.Request(f"{base_url}{route}", data=b"{}",
                                       headers={"Content-Type": "application/json"}),
                timeout=5,
            )
        assert excinfo.value.code == 404


# --- running an agent over HTTP: off unless deliberately switched on --------


def _run_request(base_url, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Lumia-Token"] = token
    request = urllib.request.Request(f"{base_url}/api/run", data=json.dumps(payload).encode(), headers=headers)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    return excinfo.value.code, json.loads(excinfo.value.read())


def test_running_is_off_when_no_token_is_configured(base_url, monkeypatch):
    """A hosted URL is reachable by anyone who has it. Default must be closed."""
    from lumia import server

    monkeypatch.setattr(server, "RUN_TOKEN", "")
    code, body = _run_request(base_url, {"role": "intake", "task": "go"})
    assert code == 503
    assert "disabled" in body["error"]
    assert "LUMIA_RUN_TOKEN" in body["fix"]


def test_a_wrong_token_is_refused(base_url, monkeypatch):
    from lumia import server

    monkeypatch.setattr(server, "RUN_TOKEN", "correct-horse")
    monkeypatch.setattr(server, "SETTINGS", replace(server.SETTINGS, anthropic_api_key="test-key"))
    code, body = _run_request(base_url, {"role": "intake", "task": "go"}, token="wrong")
    assert code == 403
    assert body["error"] == "wrong or missing token"


def test_a_missing_token_is_refused(base_url, monkeypatch):
    from lumia import server

    monkeypatch.setattr(server, "RUN_TOKEN", "correct-horse")
    monkeypatch.setattr(server, "SETTINGS", replace(server.SETTINGS, anthropic_api_key="test-key"))
    assert _run_request(base_url, {"role": "intake", "task": "go"})[0] == 403


def test_running_needs_the_model_even_with_a_valid_token(base_url, monkeypatch):
    from lumia import server

    monkeypatch.setattr(server, "RUN_TOKEN", "correct-horse")
    monkeypatch.setattr(server, "SETTINGS", replace(server.SETTINGS, anthropic_api_key=None))
    code, body = _run_request(base_url, {"role": "intake", "task": "go"}, token="correct-horse")
    assert code == 503
    assert "ANTHROPIC_API_KEY" in body["error"]


def test_an_authorised_run_still_validates_its_input(base_url, monkeypatch):
    from lumia import server

    monkeypatch.setattr(server, "RUN_TOKEN", "correct-horse")
    monkeypatch.setattr(server, "SETTINGS", replace(server.SETTINGS, anthropic_api_key="test-key"))

    code, body = _run_request(base_url, {"role": "hacker", "task": "go"}, token="correct-horse")
    assert code == 400 and "unknown agent" in body["error"]

    code, body = _run_request(base_url, {"role": "intake", "task": "   "}, token="correct-horse")
    assert code == 400


# --- operator control over HTTP: same lock as running -----------------------


def test_steps_are_not_readable_without_a_token(base_url):
    """Steps narrate the company's work; a public URL must not."""
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base_url}/api/steps/RUN-000001", timeout=5)
    assert excinfo.value.code == 403


def test_killing_is_off_without_a_token(base_url, monkeypatch):
    from lumia import server

    monkeypatch.setattr(server, "RUN_TOKEN", "")
    code, body = _run_request(base_url, {"run": "RUN-000001"})
    assert code == 503 or code == 404  # route exists but control is disabled


def test_killing_needs_the_right_token(base_url, monkeypatch):
    from lumia import server

    monkeypatch.setattr(server, "RUN_TOKEN", "correct-horse")
    request = urllib.request.Request(
        f"{base_url}/api/kill",
        data=json.dumps({"run": "RUN-000001"}).encode(),
        headers={"Content-Type": "application/json", "X-Lumia-Token": "wrong"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    assert excinfo.value.code == 403


def test_an_authorised_kill_is_accepted(base_url, monkeypatch, tmp_path):
    from lumia import server

    monkeypatch.setattr(server, "RUN_TOKEN", "correct-horse")
    monkeypatch.setattr(server, "SETTINGS", replace(server.SETTINGS, data_dir=tmp_path))

    request = urllib.request.Request(
        f"{base_url}/api/kill",
        data=json.dumps({"run": "RUN-000001", "reason": "wrong recipient"}).encode(),
        headers={"Content-Type": "application/json", "X-Lumia-Token": "correct-horse"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read())
    assert payload["killing"] == "RUN-000001"
    assert payload["reason"] == "wrong recipient"


def test_a_kill_needs_a_target(base_url, monkeypatch):
    from lumia import server

    monkeypatch.setattr(server, "RUN_TOKEN", "correct-horse")
    request = urllib.request.Request(
        f"{base_url}/api/kill",
        data=json.dumps({"run": "  "}).encode(),
        headers={"Content-Type": "application/json", "X-Lumia-Token": "correct-horse"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    assert excinfo.value.code == 400


# --- the approval queue: the gate is a queue with a person at the end -------


def _approvals_request(base_url, payload=None, token=None, method="POST"):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Lumia-Token"] = token
    data = json.dumps(payload).encode() if payload is not None else None
    return urllib.request.Request(f"{base_url}/api/approvals", data=data,
                                  headers=headers, method=method)


def _queue(monkeypatch, tmp_path, token="correct-horse"):
    """Point the server at a throwaway workspace and switch control on."""
    from lumia import server

    monkeypatch.setattr(server, "RUN_TOKEN", token)
    monkeypatch.setattr(server, "SETTINGS", replace(server.SETTINGS, data_dir=tmp_path))
    return server.Workspace.build(server.SETTINGS).approvals


def _hold(queue, body="The extra work will be $4,200."):
    from lumia.autonomy import ApprovalRequest

    return queue.submit(ApprovalRequest(
        tool="send_communication",
        arguments={"draft_id": "comm_1", "body": body},
        reason="pricing to a client needs a human",
        agent="client_comms",
    ))


def test_the_queue_is_not_readable_without_a_token(base_url, monkeypatch):
    """It holds drafted messages and recipient names — the most sensitive thing here."""
    from lumia import server

    monkeypatch.setattr(server, "RUN_TOKEN", "")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(_approvals_request(base_url, method="GET"), timeout=5)
    assert excinfo.value.code == 503


def test_a_wrong_token_cannot_read_the_queue(base_url, monkeypatch, tmp_path):
    _queue(monkeypatch, tmp_path)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(_approvals_request(base_url, token="wrong", method="GET"), timeout=5)
    assert excinfo.value.code == 403


def test_a_held_action_is_visible_to_an_operator(base_url, monkeypatch, tmp_path):
    queue = _queue(monkeypatch, tmp_path)
    held = _hold(queue)

    with urllib.request.urlopen(
        _approvals_request(base_url, token="correct-horse", method="GET"), timeout=5
    ) as response:
        payload = json.loads(response.read())

    assert payload["count"] == 1
    assert payload["pending"][0]["id"] == held.id
    assert payload["pending"][0]["reason"] == "pricing to a client needs a human"


def test_rejecting_records_the_decision_and_sends_nothing(base_url, monkeypatch, tmp_path):
    queue = _queue(monkeypatch, tmp_path)
    held = _hold(queue)

    with urllib.request.urlopen(
        _approvals_request(base_url, {"id": held.id, "decision": "reject", "note": "wrong number"},
                           token="correct-horse"), timeout=5
    ) as response:
        payload = json.loads(response.read())

    assert payload["decision"]["status"] == "rejected"
    assert payload["decision"]["decision_note"] == "wrong number"
    assert payload["executed"] is False
    assert queue.pending() == []


def test_approving_without_execute_does_not_send(base_url, monkeypatch, tmp_path):
    """Approving and sending are separate decisions, and stay separate."""
    queue = _queue(monkeypatch, tmp_path)
    held = _hold(queue)

    with urllib.request.urlopen(
        _approvals_request(base_url, {"id": held.id, "decision": "approve"},
                           token="correct-horse"), timeout=5
    ) as response:
        payload = json.loads(response.read())

    assert payload["decision"]["status"] == "approved"
    assert payload["executed"] is False
    assert queue.all()[0]["status"] == "approved"  # not "executed"


def test_an_unrecognised_decision_is_refused_rather_than_defaulted(base_url, monkeypatch, tmp_path):
    """This route can put a message in front of a client. It never guesses."""
    queue = _queue(monkeypatch, tmp_path)
    held = _hold(queue)

    for decision in ("", "maybe", "yes", "APPROVED?"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(
                _approvals_request(base_url, {"id": held.id, "decision": decision},
                                   token="correct-horse"), timeout=5)
        assert excinfo.value.code == 400
    assert queue.pending()[0]["status"] == "pending"


def test_deciding_the_same_action_twice_is_refused(base_url, monkeypatch, tmp_path):
    """Otherwise an approval clicked twice on a flaky connection sends twice."""
    queue = _queue(monkeypatch, tmp_path)
    held = _hold(queue)
    body = {"id": held.id, "decision": "approve"}

    urllib.request.urlopen(_approvals_request(base_url, body, token="correct-horse"), timeout=5)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(_approvals_request(base_url, body, token="correct-horse"), timeout=5)
    assert excinfo.value.code == 404
    assert "already approved" in json.loads(excinfo.value.read())["error"]


def test_an_unknown_approval_id_is_not_found(base_url, monkeypatch, tmp_path):
    _queue(monkeypatch, tmp_path)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            _approvals_request(base_url, {"id": "appr_nope", "decision": "approve"},
                               token="correct-horse"), timeout=5)
    assert excinfo.value.code == 404


# --- status: which keys landed, without ever showing one -------------------


def test_status_names_missing_variables_but_never_a_key(base_url, monkeypatch):
    from lumia import server

    secret = "sk-do-not-leak-this-value"
    patched = replace(server.SETTINGS)
    patched.services["email"] = replace(patched.services["email"], api_key=secret)
    monkeypatch.setattr(server, "SETTINGS", patched)

    status, raw = get(f"{base_url}/api/status")
    assert status == 200
    assert secret not in raw.decode()

    payload = json.loads(raw)
    email = next(s for s in payload["services"] if s["name"] == "email")
    assert email["live"] is True
    sms = next(s for s in payload["services"] if s["name"] == "sms")
    assert "TWILIO_AUTH_TOKEN" in sms["needs"]
