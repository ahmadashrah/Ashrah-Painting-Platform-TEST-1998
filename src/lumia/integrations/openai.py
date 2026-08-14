"""OpenAI: speech-to-text, vision and embeddings.

Used where it is the better tool rather than as a replacement brain:

- **Whisper** transcribes the voice notes the field actually sends. The spec
  requires accurate transcription of Arabic, Kurdish and French recordings,
  and until now nothing in the system did it — a transcript had to arrive
  already written.
- **Vision** describes a submitted photo so a caption can be grounded in
  what is visible instead of in what the work order says should be there.
- **Embeddings** let memory recall a past lesson by meaning rather than by
  shared keywords.

Two rules this module holds to.

**Machine output is a draft, never a verdict.** A transcript is stored as
what the recording says, not as fact about the jobsite; a vision
description is stored separately from the caption a client will read, and
it never becomes that caption on its own. Both stay subject to the same
verification and screening as anything a person typed.

**Unconfigured means simulated and flagged**, like every other integration
here — never a crash, and never silently passed off as real.
"""

from __future__ import annotations

import base64
import logging
import math
from pathlib import Path
from typing import Any

import httpx

from .base import Integration, IntegrationError

log = logging.getLogger(__name__)

#: Whisper's cap. A longer recording has to be split before sending.
MAX_AUDIO_BYTES = 25 * 1024 * 1024

#: What transcription would like, absent any other constraint. A run's own
#: budget takes precedence over this.
TRANSCRIBE_TIMEOUT_SECONDS = 90.0

#: Languages the crews actually report in, passed as a hint to improve
#: accuracy on short, noisy jobsite recordings.
FIELD_LANGUAGES = ("en", "ar", "ku", "fr", "es", "pt", "tl", "pa")


class OpenAIService(Integration):
    """Speech, vision, embeddings and chat, over the REST API.

    Deliberately not the `openai` SDK: `httpx` is already a dependency, the
    surface used here is four endpoints, and a repo that runs after
    `git clone` is worth more than the convenience.
    """

    # --- speech to text ---------------------------------------------------

    def transcribe(
        self,
        audio: bytes | str | Path,
        *,
        model: str = "whisper-1",
        language: str = "",
        prompt: str = "",
    ) -> dict[str, Any]:
        """Transcribe a recording verbatim.

        `language` is a hint, not a filter: a crew lead switching between
        Arabic and English mid-sentence should still come out intact.
        """
        mock = {
            "text": "",
            "language": language or "unknown",
            "_note": "no transcription was performed",
        }
        if not self.live:
            self._record("POST", "/audio/transcriptions", mocked=True)
            return {**mock, "_mocked": True,
                    "_reason": "openai is not configured; set OPENAI_API_KEY in .env to go live"}

        try:
            payload = _read_audio(audio, egress=self.egress)
        except (OSError, ValueError) as exc:
            return {"error": str(exc)}
        if len(payload) > MAX_AUDIO_BYTES:
            return {
                "error": f"recording is {len(payload) // 1_000_000}MB, over the "
                         f"{MAX_AUDIO_BYTES // 1_000_000}MB limit; split it before transcribing"
            }

        data: dict[str, Any] = {"model": model, "response_format": "verbose_json"}
        if language and language in FIELD_LANGUAGES:
            data["language"] = language
        if prompt:
            # Priming with site vocabulary measurably helps on proper nouns.
            data["prompt"] = prompt[:900]

        # Transcription is slow, so it asks for longer than the default — but
        # never for longer than the run actually has. Left unbounded, one
        # recording would swallow a whole communication budget on its own.
        if not self._has_time():
            return {"error": "not attempted: the run's time budget is spent"}

        try:
            response = httpx.post(
                f"{str(self.credentials.base_url).rstrip('/')}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.credentials.api_key}"},
                files={"file": ("recording.m4a", payload, "application/octet-stream")},
                data=data,
                timeout=self._timeout(TRANSCRIBE_TIMEOUT_SECONDS),
            )
        except httpx.HTTPError as exc:
            raise IntegrationError(f"openai transcription failed: {exc}") from exc

        self._record("POST", "/audio/transcriptions", mocked=False)
        if response.status_code >= 400:
            raise IntegrationError(f"openai transcription HTTP {response.status_code}: {response.text[:300]}")

        body = response.json()
        return {
            "text": str(body.get("text", "")).strip(),
            "language": body.get("language", language or "unknown"),
            "duration_seconds": body.get("duration"),
            "verified": True,
        }

    # --- vision -------------------------------------------------------------

    def describe_image(
        self,
        image: bytes | str | Path,
        *,
        model: str = "gpt-4o-mini",
        prompt: str = "",
    ) -> dict[str, Any]:
        """Describe what is visible in a photo. Describes, does not conclude."""
        instruction = prompt or (
            "Describe only what is visibly present in this construction photo: the surface, "
            "its apparent condition, the stage of any coating work, and anything unsafe or "
            "sensitive such as people, documents, screens, keys or security equipment. "
            "Do not infer whether work is complete, and do not guess at anything outside the frame."
        )
        mock = {
            "description": "",
            "_note": "no image analysis was performed",
        }
        if not self.live:
            self._record("POST", "/chat/completions", mocked=True)
            return {**mock, "_mocked": True,
                    "_reason": "openai is not configured; set OPENAI_API_KEY in .env to go live"}

        url = image if isinstance(image, str) and image.startswith(("http://", "https://")) else None
        if url is not None and self.egress is not None:
            # The URL is handed to the vision API to fetch. It is still this
            # run reaching for an address the model chose.
            self.egress.check(url, what="reading a photo")
        if url is None:
            try:
                encoded = base64.b64encode(_read_bytes(image)).decode()
            except (OSError, ValueError) as exc:
                return {"error": str(exc)}
            url = f"data:image/jpeg;base64,{encoded}"

        body = self.request(
            "POST",
            "/chat/completions",
            json={
                "model": model,
                "max_tokens": 400,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }],
            },
            mock=mock,
        )
        choices = body.get("choices") or []
        text = ""
        if choices:
            text = str((choices[0].get("message") or {}).get("content") or "").strip()
        return {"description": text, "verified": bool(text), "model": model}

    # --- embeddings ----------------------------------------------------------

    def embed(self, texts: list[str], *, model: str = "text-embedding-3-small") -> dict[str, Any]:
        """Vectors for a batch of strings, in the order given."""
        clean = [t.strip() for t in texts if t and t.strip()]
        if not clean:
            return {"vectors": [], "verified": False}
        body = self.request(
            "POST",
            "/embeddings",
            json={"model": model, "input": clean},
            mock={"data": []},
        )
        vectors = [item.get("embedding", []) for item in (body.get("data") or [])]
        return {"vectors": vectors, "verified": len(vectors) == len(clean), "model": model}

    # --- chat (used by the agent loop through llm.OpenAIClient) ---------------

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/chat/completions", json=payload, mock={"choices": []})


# --- helpers ----------------------------------------------------------------


def _read_bytes(source: bytes | str | Path) -> bytes:
    if isinstance(source, bytes):
        return source
    path = Path(source)
    if not path.is_file():
        raise ValueError(f"no file at {source}")
    return path.read_bytes()


def _read_audio(source: bytes | str | Path, egress: Any = None) -> bytes:
    if isinstance(source, bytes):
        return source
    text = str(source)
    if text.startswith(("http://", "https://")):
        if egress is not None:
            egress.check(text, what="fetching a recording")
        try:
            response = httpx.get(text, timeout=60.0, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise ValueError(f"could not fetch the recording at {text}: {exc}") from exc
        if response.status_code >= 400:
            raise ValueError(f"recording at {text} returned HTTP {response.status_code}")
        return response.content
    return _read_bytes(source)


def cosine(a: list[float], b: list[float]) -> float:
    """Similarity between two vectors, 0 when either is empty or degenerate."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
