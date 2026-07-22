"""Agent-composed SSE stream (plan.md phase 9, D10 priority 3, direction B).

The agent resolves a request with `{"stream": true, "chunks": [...]}` where
`chunks` are *pre-framed* SSE event strings. cc-mock joins them verbatim
(agent-agnostic -- it never interprets or reframes the payload), so any
provider's wire shape round-trips: OpenAI (`data: ...` + `data: [DONE]`) and
Anthropic (named `event: ...` events) alike.
"""

from __future__ import annotations

import asyncio

import pytest

import cc_mock_server.cli as cli
from cc_mock_server.agent_handler import AgentHandler
from cc_mock_server.config import Config
from cc_mock_server.enums import AgentMode
from cc_mock_server.models import Request, Response
from cc_mock_server.streaming import parse_sse_events

OPENAI_CHUNKS = [
    'data: {"choices":[{"delta":{"content":"hel"}}]}',
    'data: {"choices":[{"delta":{"content":"lo"}}]}',
    "data: [DONE]",
]
ANTHROPIC_CHUNKS = [
    'event: message_start\ndata: {"type":"message_start"}',
    'event: content_block_delta\ndata: {"delta":{"text":"hi"}}',
    'event: message_stop\ndata: {"type":"message_stop"}',
]


def _make_request() -> Request:
    return Request.from_raw(method="GET", url="http://x/v1/chat", host="x", path="/v1/chat")


# --------------------------------------------------------------------------
# Response.from_chunks: pure framing (direction B -- verbatim join)
# --------------------------------------------------------------------------


class TestResponseFromChunks:
    def test_frames_openai_style_chunks(self):
        r = Response.from_chunks(OPENAI_CHUNKS)
        assert r.is_stream is True
        assert r.status_code == 200
        assert r.content_type == "text/event-stream"
        assert r.is_json is False
        assert r.headers.get("content-type") == "text/event-stream"
        assert parse_sse_events(r.body) == OPENAI_CHUNKS

    def test_anthropic_named_events_round_trip_verbatim(self):
        r = Response.from_chunks(ANTHROPIC_CHUNKS)
        # `event:` lines are preserved -- cc-mock never reframes the payload.
        assert parse_sse_events(r.body) == ANTHROPIC_CHUNKS

    def test_status_and_headers_override(self):
        r = Response.from_chunks(OPENAI_CHUNKS, status_code=201, headers={"x-req": "1"})
        assert r.status_code == 201
        assert r.headers["x-req"] == "1"
        assert r.headers["content-type"] == "text/event-stream"

    def test_content_type_override_preserved(self):
        r = Response.from_chunks(OPENAI_CHUNKS, content_type="text/event-stream; charset=utf-8")
        assert r.content_type == "text/event-stream; charset=utf-8"

    def test_empty_chunks_is_empty_streaming_body(self):
        r = Response.from_chunks([])
        assert r.body == ""
        assert r.is_stream is True


# --------------------------------------------------------------------------
# agent_handler: pending respond(chunks=...) + sync envelope shaping
# --------------------------------------------------------------------------


class TestPendingRespondWithChunks:
    @pytest.mark.asyncio
    async def test_respond_chunks_resolves_streaming_response(self):
        handler = AgentHandler(Config(agent_mode=AgentMode.PENDING, agent_timeout=5.0))

        async def drive() -> None:
            for _ in range(200):
                if handler.pending:
                    break
                await asyncio.sleep(0.001)
            request_id = next(iter(handler.pending))
            assert handler.respond(request_id, 200, None, chunks=OPENAI_CHUNKS) is True

        result, _ = await asyncio.gather(handler.handle(_make_request()), drive())
        await handler.aclose()

        assert result.action == "respond"
        assert result.response.is_stream is True
        assert result.response.content_type == "text/event-stream"
        assert parse_sse_events(result.response.body) == OPENAI_CHUNKS


class TestSyncEnvelopeShaping:
    def test_response_from_agent_json_stream_chunks(self):
        handler = AgentHandler(Config())
        r = handler._response_from_agent_json(
            {"stream": True, "chunks": ANTHROPIC_CHUNKS, "status": 200}
        )
        assert r.is_stream is True
        assert parse_sse_events(r.body) == ANTHROPIC_CHUNKS

    def test_chunks_present_without_stream_key_still_streams(self):
        handler = AgentHandler(Config())
        r = handler._response_from_agent_json({"chunks": OPENAI_CHUNKS})
        assert r.is_stream is True
        assert parse_sse_events(r.body) == OPENAI_CHUNKS

    def test_stream_true_without_chunks_is_malformed(self):
        handler = AgentHandler(Config())
        with pytest.raises(ValueError):
            handler._response_from_agent_json({"stream": True})

    def test_chunks_not_a_list_is_malformed(self):
        handler = AgentHandler(Config())
        with pytest.raises(ValueError):
            handler._response_from_agent_json({"stream": True, "chunks": "nope"})

    def test_chunks_with_non_string_items_is_malformed(self):
        handler = AgentHandler(Config())
        with pytest.raises(ValueError):
            handler._response_from_agent_json({"chunks": ["data: ok", 123]})

    def test_non_stream_body_response_unchanged(self):
        handler = AgentHandler(Config())
        r = handler._response_from_agent_json({"status": 201, "body": {"id": "ch_1"}})
        assert r.is_stream is False
        assert r.status_code == 201
        assert r.is_json is True


# --------------------------------------------------------------------------
# CLI: `cc-mock respond --chunk ...` builds a streaming envelope
# --------------------------------------------------------------------------


class TestCliRespondChunks:
    def test_chunk_flags_build_stream_payload(self, monkeypatch):
        captured: dict = {}

        def fake_request(args, method, path, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["json"] = kwargs.get("json")
            return 0

        monkeypatch.setattr(cli, "_request", fake_request)
        parser = cli.build_arg_parser()
        args = parser.parse_args(
            ["respond", "--request-id", "abc", "--chunk", "data: a", "--chunk", "data: [DONE]"]
        )
        assert cli.cmd_respond(args) == 0
        assert captured["method"] == "POST"
        assert captured["path"] == "/mock/respond"
        assert captured["json"]["chunks"] == ["data: a", "data: [DONE]"]
        assert captured["json"]["stream"] is True
        assert "body" not in captured["json"]

    def test_json_and_chunk_are_mutually_exclusive(self, monkeypatch):
        monkeypatch.setattr(cli, "_request", lambda *a, **k: 0)
        parser = cli.build_arg_parser()
        args = parser.parse_args(
            ["respond", "--request-id", "abc", "--json", "{}", "--chunk", "data: a"]
        )
        assert cli.cmd_respond(args) != 0

    def test_respond_without_json_or_chunk_errors(self, monkeypatch):
        monkeypatch.setattr(cli, "_request", lambda *a, **k: 0)
        parser = cli.build_arg_parser()
        args = parser.parse_args(["respond", "--request-id", "abc"])
        assert cli.cmd_respond(args) != 0

    def test_plain_json_respond_still_works(self, monkeypatch):
        captured: dict = {}

        def fake_request(args, method, path, **kwargs):
            captured["json"] = kwargs.get("json")
            return 0

        monkeypatch.setattr(cli, "_request", fake_request)
        parser = cli.build_arg_parser()
        args = parser.parse_args(
            ["respond", "--request-id", "abc", "--status", "200", "--json", '{"result": "ok"}']
        )
        assert cli.cmd_respond(args) == 0
        assert captured["json"]["body"] == {"result": "ok"}
        assert "chunks" not in captured["json"]
