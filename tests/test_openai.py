"""OpenAI: transcription, vision, embeddings, and GPT behind the same loop."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from lumia.comms.seed import seed_demo_projects
from lumia.config import ServiceCredentials, provider_for
from lumia.integrations.openai import OpenAIService, cosine
from lumia.llm import OpenAIClient, _from_openai, _to_openai_messages, build_client
from lumia.memory import Memory
from lumia.tools import Toolbox


@pytest.fixture
def project(ws):
    return seed_demo_projects(ws)["project_id"]


@pytest.fixture
def box(ws):
    return Toolbox(ws)


# --- provider selection ----------------------------------------------------


@pytest.mark.parametrize(
    "model, provider",
    [
        ("claude-opus-5", "anthropic"),
        ("claude-sonnet-5", "anthropic"),
        ("gpt-4o", "openai"),
        ("gpt-4o-mini", "openai"),
        ("o3-mini", "openai"),
        ("something-unknown", "anthropic"),
    ],
)
def test_the_model_name_picks_the_provider(model, provider):
    assert provider_for(model) == provider


def test_build_client_follows_the_model(settings):
    from lumia.llm import ClaudeClient

    assert isinstance(build_client(settings), ClaudeClient)
    assert isinstance(build_client(replace(settings, model="gpt-4o")), OpenAIClient)


def test_a_gpt_model_without_a_key_says_exactly_that(settings):
    from lumia.llm import MissingAPIKey

    client = build_client(replace(settings, model="gpt-4o"))
    with pytest.raises(MissingAPIKey) as excinfo:
        client.create(system="s", messages=[{"role": "user", "content": "hi"}])
    assert "OPENAI_API_KEY" in str(excinfo.value)


# --- translating to OpenAI and back ----------------------------------------


def test_tool_results_become_tool_role_messages():
    """Anthropic puts tool results in a user turn; OpenAI wants their own role."""
    messages = [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "calling it"},
            {"type": "tool_use", "id": "call_1", "name": "list_projects", "input": {"status": "active"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": '{"count": 1}'},
        ]},
    ]
    converted = _to_openai_messages(messages)

    assert converted[0] == {"role": "user", "content": "do the thing"}

    assistant = converted[1]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert assistant["tool_calls"][0]["function"]["name"] == "list_projects"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"status": "active"}

    assert converted[2] == {"role": "tool", "tool_call_id": "call_1", "content": '{"count": 1}'}


def test_a_completion_becomes_blocks_the_loop_can_read():
    response = _from_openai({
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "content": "I'll check the projects.",
                "tool_calls": [{
                    "id": "call_9",
                    "function": {"name": "list_projects", "arguments": '{"status":"active"}'},
                }],
            },
        }]
    })

    assert response.stop_reason == "tool_use"
    assert response.content[0].type == "text"
    assert response.content[1].type == "tool_use"
    assert response.content[1].name == "list_projects"
    assert response.content[1].input == {"status": "active"}


def test_unparsable_tool_arguments_do_not_kill_the_run():
    """The model sometimes emits invalid JSON. An empty input is recoverable."""
    response = _from_openai({
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {"tool_calls": [{"id": "c1", "function": {"name": "list_projects", "arguments": "{not json"}}]},
        }]
    })
    assert response.content[0].type == "tool_use"
    assert response.content[0].input == {}


@pytest.mark.parametrize(
    "finish, stop",
    [("stop", "end_turn"), ("tool_calls", "tool_use"), ("length", "max_tokens"), ("content_filter", "refusal")],
)
def test_finish_reasons_map_onto_the_loops_vocabulary(finish, stop):
    response = _from_openai({"choices": [{"finish_reason": finish, "message": {"content": "x"}}]})
    assert response.stop_reason == stop


def test_an_empty_completion_is_handled():
    assert _from_openai({"choices": []}).content[0].text == ""
    assert _from_openai({"choices": [], "_mocked": True}).stop_reason == "mocked"


def test_the_translated_loop_still_hits_the_autonomy_gate(ws, project):
    """The whole point of adapting at the edge: one gate, both providers."""
    from lumia.agent import Agent
    from lumia.domain.projects import DeliveryStatus

    box = Toolbox(ws)
    draft = box.call("draft_communication", {
        "project_id": project, "recipient": "Dana Whitfield (DEMO)", "kind": "daily_log",
        "subject": "Update", "body": "Primer applied. The extra ceiling work will be $4,200.",
    })

    class GPTShaped:
        """Returns raw OpenAI bodies, translated on the way out."""

        def __init__(self):
            self.bodies = [
                {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
                    {"id": "c1", "function": {"name": "send_communication",
                                              "arguments": json.dumps({"draft_id": draft["id"]})}}]}}]},
                {"choices": [{"finish_reason": "stop", "message": {"content": "Queued for approval."}}]},
            ]

        def create(self, **_kwargs):
            return _from_openai(self.bodies.pop(0))

    run = Agent(role="client_comms", workspace=ws, toolbox=box, client=GPTShaped()).run("send it")

    assert len(run.approvals_raised) == 1
    assert run.tool_calls[0].executed is False
    assert ws.comms.get_communication(draft["id"])["status"] == DeliveryStatus.AWAITING_APPROVAL.value


# --- transcription ----------------------------------------------------------


def test_transcription_is_refused_without_a_key(ws, box, project):
    submission = ws.comms.submissions_for(project)[0]
    ws.store.patch("submissions", submission["id"], {"transcript": ""})

    result = box.call("transcribe_field_submission", {
        "submission_id": submission["id"], "audio_uri": "https://example.invalid/a.m4a"})

    assert "error" in result
    assert "OPENAI_API_KEY" in result["error"]
    # And it says not to invent one.
    assert "Do not write a transcript" in result["guidance"]


def test_an_existing_transcript_is_never_overwritten(ws, box, project):
    submission = ws.comms.submissions_for(project)[0]
    original = submission["transcript"]

    result = box.call("transcribe_field_submission", {"submission_id": submission["id"]})
    assert "error" in result
    assert ws.comms.get_submission(submission["id"])["transcript"] == original


def test_a_transcript_can_be_attached_once(ws, project):
    submission = ws.comms.submissions_for(project)[0]
    ws.store.patch("submissions", submission["id"], {"transcript": ""})

    saved = ws.comms.attach_transcript(submission["id"], "We finished the corridor.", source="whisper")
    assert saved["transcript"] == "We finished the corridor."
    assert [r["action"] for r in saved["revisions"]][-1] == "transcribed"

    again = ws.comms.attach_transcript(submission["id"], "Something else")
    assert "error" in again
    assert ws.comms.get_submission(submission["id"])["transcript"] == "We finished the corridor."


def test_an_empty_transcription_is_not_stored(ws, project):
    submission = ws.comms.submissions_for(project)[0]
    ws.store.patch("submissions", submission["id"], {"transcript": ""})
    assert "error" in ws.comms.attach_transcript(submission["id"], "   ")


def test_transcription_falls_back_to_mock_when_unconfigured():
    service = OpenAIService(ServiceCredentials(name="openai", api_key=None))
    result = service.transcribe(b"fake audio")
    assert result["_mocked"] is True
    assert result["text"] == ""


# --- vision ------------------------------------------------------------------


def test_describing_media_is_refused_without_a_key(ws, box, project):
    media = ws.comms.media_for(project)[0]
    result = box.call("describe_media", {"media_id": media["id"]})
    assert "error" in result
    assert "not from assumption" in result["guidance"]


def test_vision_never_captions_on_its_own(ws, box, project, monkeypatch):
    """A model reading an image can be wrong; the caption is Ashrah's statement."""
    media = ws.comms.media_for(project)[0]
    monkeypatch.setattr(type(ws.openai), "live", property(lambda self: True))
    monkeypatch.setattr(
        type(ws.openai), "describe_image",
        lambda self, image, **kw: {"description": "A corridor wall with primer applied. A worker's face is visible."},
    )

    result = box.call("describe_media", {"media_id": media["id"]})

    assert "primer" in result["description"]
    assert "personal_information" in result["possible_flags"]
    # The description is stored, but the caption is untouched.
    assert ws.comms.get_media_asset(media["id"])["caption"] == ""
    assert "write the caption yourself" in result["guidance"].lower()


# --- embeddings ---------------------------------------------------------------


def test_cosine_handles_degenerate_input():
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_memory_falls_back_to_keywords_without_an_embedder(ws):
    """A memory that goes silent when a key is missing would never be noticed."""
    from lumia.memory import Lesson

    ws.memory.record_lesson(Lesson(
        observation="Northgate replies faster to short emails",
        hypothesis="Short emails get read on site",
        evidence="four threads",
        segment="general_contractor",
    ))
    assert ws.memory.semantic is False
    assert ws.memory.recall(query="short emails")


def test_semantic_recall_ranks_by_meaning(ws):
    """Different words, same subject — keyword overlap would miss this."""
    from lumia.memory import Lesson

    class FakeEmbedder:
        live = True

        def embed(self, texts, model=""):
            # "site radio silence" and "stopped replying" share no keywords,
            # but sit on the same axis here.
            def vector(t):
                t = t.lower()
                quiet = any(w in t for w in ("silence", "stopped replying", "unresponsive"))
                money = any(w in t for w in ("price", "invoice", "cost"))
                return [1.0 if quiet else 0.0, 1.0 if money else 0.0, 0.1]
            return {"vectors": [vector(t) for t in texts]}

    memory = Memory(ws.store, embedder=FakeEmbedder(), model="test-embed")
    memory.record_lesson(Lesson(
        observation="The GC stopped replying after the third long email",
        hypothesis="Long emails go unread on site", evidence="three threads"))
    memory.record_lesson(Lesson(
        observation="Invoices are queried when they arrive without a breakdown",
        hypothesis="Itemise the invoice", evidence="two disputes"))

    assert memory.semantic is True
    hits = memory.recall(query="site radio silence from the contractor")
    assert hits
    assert "stopped replying" in hits[0]["observation"]
    assert hits[0]["matched_by"] == "meaning"
    # Vectors are storage, not something callers should see.
    assert "embedding" not in hits[0]


def test_lessons_stored_before_embeddings_are_backfilled(ws):
    """Turning embeddings on must not make the oldest lessons invisible."""
    from lumia.memory import Lesson

    plain = Memory(ws.store)
    plain.record_lesson(Lesson(observation="An old lesson about access delays",
                               hypothesis="Access delays cost mornings", evidence="logs"))
    assert "embedding" not in ws.store.list("lessons")[0]

    class FakeEmbedder:
        live = True

        def embed(self, texts, model=""):
            return {"vectors": [[1.0, 0.0] for _ in texts]}

    smart = Memory(ws.store, embedder=FakeEmbedder(), model="test-embed")
    hits = smart.recall(query="access delays")

    assert hits and hits[0]["matched_by"] == "meaning"
    assert ws.store.list("lessons")[0]["embedding"] == [1.0, 0.0]


def test_a_failing_embedder_degrades_instead_of_raising(ws):
    from lumia.memory import Lesson

    class Broken:
        live = True

        def embed(self, texts, model=""):
            raise RuntimeError("rate limited")

    memory = Memory(ws.store, embedder=Broken())
    memory.record_lesson(Lesson(observation="Short emails work", hypothesis="brevity", evidence="x"))
    assert memory.recall(query="short emails")   # keyword path still answers
