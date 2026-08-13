"""The model clients, and the boundary that keeps the loop provider-agnostic.

`Agent` speaks one dialect: content blocks with a `type`, `tool_use` blocks
carrying `name` and `input`, and a `stop_reason`. Rather than teach the
loop a second dialect, `OpenAIClient` translates at the edge — it accepts
the same call `ClaudeClient` does and returns the same shape. The autonomy
gate, the tool dispatch and the approval queue are then identical on either
provider, which is the point: the safety machinery must not have two
implementations, one of which is less tested.

Which client you get follows from the model name alone (see
`config.provider_for`), so moving an agent across is one environment
variable and no code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .config import Settings, provider_for

log = logging.getLogger(__name__)

#: Non-streaming ceiling. Above roughly this, use streaming instead or the
#: request risks an HTTP timeout.
DEFAULT_MAX_TOKENS = 16_000


class MissingAPIKey(RuntimeError):
    pass


class ClaudeClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None

    def _ensure(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise MissingAPIKey("the `anthropic` package is not installed; run `pip install -r requirements.txt`") from exc

        if not self.settings.anthropic_api_key:
            raise MissingAPIKey(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key, "
                "or export it in your shell."
            )
        self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Any:
        client = self._ensure()
        request: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": max_tokens,
            # Cache the system prompt: it's identical on every turn of a run.
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.settings.effort},
        }
        if tools:
            request["tools"] = tools
        return client.messages.create(**request)


# --- OpenAI, presented in the shape the loop already understands ------------


@dataclass
class Block:
    """One content block, matching the attribute names the loop reads."""

    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class Response:
    content: list[Block]
    stop_reason: str = "end_turn"
    stop_details: Any = None


class OpenAIClient:
    """A GPT model behind the same interface as `ClaudeClient`.

    Translation is mechanical in both directions:

    - **Out**: the system prompt becomes a system message; Anthropic tool
      results (which ride inside a user turn) become the `tool` role
      messages OpenAI expects; tool schemas get wrapped in `{"type":
      "function"}`.
    - **Back**: `tool_calls` become `tool_use` blocks with parsed arguments,
      and `finish_reason` maps onto `stop_reason`.

    Malformed tool arguments are turned into an empty input rather than an
    exception. The model occasionally emits invalid JSON, and a tool that
    receives nothing produces a clear error the model can recover from; a
    crash loses the whole run.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        from .integrations.openai import OpenAIService

        self.service = OpenAIService(settings.service("openai"))

    def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Response:
        if not self.service.live:
            raise MissingAPIKey(
                f"ASHRAH_MODEL is '{self.settings.model}', which is an OpenAI model, but "
                "OPENAI_API_KEY is not set. Add it to .env, or set ASHRAH_MODEL back to a "
                "Claude model."
            )

        payload: dict[str, Any] = {
            "model": self.settings.model,
            "max_completion_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}] + _to_openai_messages(messages),
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                    },
                }
                for t in tools
            ]
        return _from_openai(self.service.chat(payload))


def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-shaped turns → OpenAI chat messages."""
    converted: list[dict[str, Any]] = []
    for message in messages:
        role, content = message.get("role"), message.get("content")

        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue

        if role == "user":
            # A user turn holds tool results, plain text, or both.
            text_parts, results = [], []
            for block in content or []:
                kind = _block_get(block, "type")
                if kind == "tool_result":
                    results.append({
                        "role": "tool",
                        "tool_call_id": _block_get(block, "tool_use_id"),
                        "content": _block_get(block, "content") or "",
                    })
                elif kind == "text":
                    text_parts.append(_block_get(block, "text"))
            converted.extend(results)
            if text_parts:
                converted.append({"role": "user", "content": "\n".join(text_parts)})
            continue

        # Assistant turn: text plus any tool calls it made.
        text_parts, calls = [], []
        for block in content or []:
            kind = _block_get(block, "type")
            if kind == "text":
                text_parts.append(_block_get(block, "text"))
            elif kind == "tool_use":
                calls.append({
                    "id": _block_get(block, "id"),
                    "type": "function",
                    "function": {
                        "name": _block_get(block, "name"),
                        "arguments": json.dumps(_block_get(block, "input") or {}, default=str),
                    },
                })
        turn: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
        if calls:
            turn["tool_calls"] = calls
        converted.append(turn)
    return converted


def _from_openai(body: dict[str, Any]) -> Response:
    """An OpenAI completion → the block shape the loop reads."""
    choices = body.get("choices") or []
    if not choices:
        reason = "mocked" if body.get("_mocked") else "empty_response"
        return Response(content=[Block(type="text", text="")], stop_reason=reason)

    choice = choices[0]
    message = choice.get("message") or {}
    blocks: list[Block] = []

    if message.get("content"):
        blocks.append(Block(type="text", text=str(message["content"])))

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw)
        except ValueError:
            # Invalid JSON from the model: hand the tool an empty input and
            # let it return its own error rather than killing the run.
            log.warning("openai returned unparsable arguments for %s: %.120s", function.get("name"), raw)
            arguments = {}
        blocks.append(Block(
            type="tool_use",
            id=str(call.get("id") or ""),
            name=str(function.get("name") or ""),
            input=arguments if isinstance(arguments, dict) else {},
        ))

    finish = str(choice.get("finish_reason") or "stop")
    stop_reason = {
        "tool_calls": "tool_use",
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "refusal",
    }.get(finish, finish)
    return Response(content=blocks or [Block(type="text", text="")], stop_reason=stop_reason)


def _block_get(block: Any, key: str) -> Any:
    """Blocks arrive as dicts from us and as objects from the SDK."""
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


def build_client(settings: Settings) -> Any:
    """The client for whichever provider this run's model belongs to."""
    if provider_for(settings.model) == "openai":
        return OpenAIClient(settings)
    return ClaudeClient(settings)


def text_of(response: Any) -> str:
    """Concatenate the text blocks of a response."""
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def tool_uses(response: Any) -> list[Any]:
    return [b for b in getattr(response, "content", []) or [] if getattr(b, "type", None) == "tool_use"]
