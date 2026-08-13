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


def test_the_api_cannot_send_anything(base_url):
    """There is no write path: the send tool is not reachable over HTTP."""
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
