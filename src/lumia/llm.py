"""Thin wrapper around the Claude API.

Keeps model configuration in one place: adaptive thinking, effort, and a
cache breakpoint on the system prompt (which is byte-stable across a run,
so every turn after the first reads it from cache instead of reprocessing
it).
"""

from __future__ import annotations

import logging
from typing import Any

from .config import Settings

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


def text_of(response: Any) -> str:
    """Concatenate the text blocks of a response."""
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def tool_uses(response: Any) -> list[Any]:
    return [b for b in getattr(response, "content", []) or [] if getattr(b, "type", None) == "tool_use"]
