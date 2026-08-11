from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from lumia.config import ServiceCredentials, Settings
from lumia.workspace import Workspace


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings with every integration unconfigured, so nothing hits the network."""
    names = (
        "crm", "email", "sms", "calendar", "search", "construction_data", "weather",
        "operations", "timeclock",
    )
    return Settings(
        anthropic_api_key="test-key",
        model="claude-opus-5",
        effort="high",
        company_name="Ashrah Painting Ltd.",
        company_phone="",
        company_email="growth@example.com",
        data_dir=tmp_path,
        services={n: ServiceCredentials(name=n, api_key=None, base_url="https://example.invalid") for n in names},
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
