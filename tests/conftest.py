from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from lumia.config import SETTINGS, Settings
from lumia.workspace import Workspace


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings with every integration unconfigured, so nothing hits the network.

    The service definitions are taken from the real ones with their keys
    blanked, rather than hand-written here — a fixture that invents its own
    credential shape stops catching changes to the real one.
    """
    services = {
        name: replace(credentials, api_key=None, base_url="https://example.invalid", extra={})
        for name, credentials in SETTINGS.services.items()
    }
    return Settings(
        anthropic_api_key="test-key",
        model="claude-opus-5",
        effort="high",
        company_name="Ashrah Painting Ltd.",
        company_phone="",
        company_email="growth@example.com",
        data_dir=tmp_path,
        services=services,
    )


@pytest.fixture
def ws(settings, tmp_path) -> Workspace:
    return Workspace.build(settings=settings, data_dir=tmp_path)


# --- fake Claude client --------------------------------------------------


@dataclass
class FakeBlock:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeResponse:
    content: list[FakeBlock]
    stop_reason: str = "end_turn"
    stop_details: Any = None


class FakeClient:
    """Replays a scripted sequence of responses instead of calling the API."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def create(self, *, system: str, messages: list[dict[str, Any]], tools=None, max_tokens: int = 0) -> FakeResponse:
        self.requests.append({"system": system, "messages": list(messages), "tools": tools})
        if not self.responses:
            return FakeResponse(content=[FakeBlock(type="text", text="done")])
        return self.responses.pop(0)


def text(message: str) -> FakeResponse:
    return FakeResponse(content=[FakeBlock(type="text", text=message)])


def calls_tool(tool: str, arguments: dict[str, Any], use_id: str = "toolu_1") -> FakeResponse:
    return FakeResponse(
        content=[FakeBlock(type="tool_use", id=use_id, name=tool, input=arguments)],
        stop_reason="tool_use",
    )
