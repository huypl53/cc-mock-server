"""RED-first unit tests for `cc_mock_server.streaming` (plan.md phase 8,
D10). Pure functions only -- no mitmproxy, no event loop; the real
mitmproxy-backed pass-through tee/replay behavior is covered by
`tests/test_streaming_integration.py`.

Also covers the two other pieces of "model surface" phase 8 touches that
have no dedicated integration test of their own: `Response.is_stream`
round-tripping through the real `Recorder` (save -> load), and `Config`'s
new `capture_streams`/`stream_delay` fields (defaults + validation).
`tests/test_models.py`/`tests/test_config.py` are owned by earlier phases
and are intentionally left untouched -- these belong here instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cc_mock_server.config import Config
from cc_mock_server.models import Recording, RecordingMetadata, Request, Response
from cc_mock_server.recorder import Recorder
from cc_mock_server.streaming import frame_sse_events, is_sse, parse_sse_events

# --------------------------------------------------------------------------
# is_sse
# --------------------------------------------------------------------------


class TestIsSse:
    def test_positive_exact_content_type(self):
        assert is_sse({"content-type": "text/event-stream"}) is True

    def test_positive_with_charset_suffix(self):
        assert is_sse({"Content-Type": "text/event-stream; charset=utf-8"}) is True

    def test_positive_case_insensitive_header_name(self):
        assert is_sse({"CONTENT-TYPE": "text/event-stream"}) is True

    def test_negative_json_content_type(self):
        assert is_sse({"content-type": "application/json"}) is False

    def test_negative_missing_content_type_header(self):
        assert is_sse({"x-custom": "whatever"}) is False

    def test_negative_empty_headers(self):
        assert is_sse({}) is False

    def test_negative_plain_text_is_not_sse(self):
        # text/event-stream is a distinct, exact media type -- "text/plain"
        # (or any other text/* type) must not be mistaken for SSE.
        assert is_sse({"content-type": "text/plain"}) is False


# --------------------------------------------------------------------------
# parse_sse_events / frame_sse_events
# --------------------------------------------------------------------------


class TestParseSseEvents:
    def test_splits_on_blank_line(self):
        body = 'data: {"a": 1}\n\ndata: {"a": 2}\n\n'
        assert parse_sse_events(body) == ['data: {"a": 1}', 'data: {"a": 2}']

    def test_keeps_done_sentinel_as_its_own_event(self):
        body = 'data: {"a": 1}\n\ndata: [DONE]\n\n'
        assert parse_sse_events(body) == ['data: {"a": 1}', "data: [DONE]"]

    def test_keeps_multi_line_event_intact(self):
        # An event with an "event:" field plus a "data:" field must survive
        # as ONE list entry, not be split further.
        body = 'event: message\ndata: {"a": 1}\n\ndata: [DONE]\n\n'
        assert parse_sse_events(body) == ['event: message\ndata: {"a": 1}', "data: [DONE]"]

    def test_normalizes_crlf_line_endings(self):
        body = 'data: {"a": 1}\r\n\r\ndata: [DONE]\r\n\r\n'
        assert parse_sse_events(body) == ['data: {"a": 1}', "data: [DONE]"]

    def test_empty_body_yields_no_events(self):
        assert parse_sse_events("") == []

    def test_no_trailing_blank_line_still_parses(self):
        body = 'data: {"a": 1}\n\ndata: [DONE]'
        assert parse_sse_events(body) == ['data: {"a": 1}', "data: [DONE]"]


class TestFrameSseEvents:
    def test_frames_events_with_blank_line_separators(self):
        events = ['data: {"a": 1}', "data: [DONE]"]
        framed = frame_sse_events(events)
        assert framed == b'data: {"a": 1}\n\ndata: [DONE]\n\n'

    def test_empty_events_yields_empty_bytes(self):
        assert frame_sse_events([]) == b""

    def test_round_trips_with_parse_sse_events(self):
        events = ['data: {"a": 1}', 'data: {"a": 2}', "data: [DONE]"]
        framed = frame_sse_events(events)
        assert parse_sse_events(framed.decode("utf-8")) == events


# --------------------------------------------------------------------------
# Response.is_stream (model + recorder round-trip)
# --------------------------------------------------------------------------


class TestResponseIsStreamField:
    def test_defaults_to_false(self):
        response = Response(status_code=200, body="ok")
        assert response.is_stream is False

    def test_from_raw_accepts_is_stream_kwarg(self):
        response = Response.from_raw(
            status_code=200,
            raw_body=b'data: {"a": 1}\n\ndata: [DONE]\n\n',
            content_type="text/event-stream",
            is_stream=True,
        )
        assert response.is_stream is True
        # SSE is text (D8: text/* is always text-safe) -- never base64.
        assert response.is_json is False
        assert response.body == 'data: {"a": 1}\n\ndata: [DONE]\n\n'

    def test_json_round_trip_preserves_is_stream(self):
        response = Response.from_raw(
            status_code=200,
            raw_body=b"data: [DONE]\n\n",
            content_type="text/event-stream",
            is_stream=True,
        )
        reloaded = Response.model_validate_json(response.model_dump_json())
        assert reloaded.is_stream is True
        assert reloaded == response


class TestRecorderRoundTripsIsStream:
    @pytest.mark.asyncio
    async def test_save_then_reload_preserves_is_stream(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        request = Request(
            method="POST",
            url="http://api.example.com/v1/stream",
            host="api.example.com",
            path="/v1/stream",
        )
        response = Response.from_raw(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            raw_body=b'data: {"a": 1}\n\ndata: [DONE]\n\n',
            content_type="text/event-stream",
            is_stream=True,
        )

        saved = await recorder.save(request, response, source="live")
        assert saved.response.is_stream is True

        # Force a fresh load from disk (a second Recorder instance, like a
        # process restart) -- the round trip must survive serialization,
        # not just the in-memory copy `save()` already returns.
        reloaded_recorder = Recorder(tmp_path)
        reloaded = reloaded_recorder.load_all()
        assert len(reloaded) == 1
        assert reloaded[0].response.is_stream is True
        assert reloaded[0].response.body == 'data: {"a": 1}\n\ndata: [DONE]\n\n'
        assert reloaded[0].response.content_type == "text/event-stream"

    def test_old_recording_without_is_stream_field_defaults_false(self, tmp_path: Path):
        """A recording written before phase 8 (no `is_stream` key at all in
        its JSON) must still load -- defaulting to False -- rather than
        failing validation (backward compatibility)."""
        host_dir = tmp_path / "api.example.com"
        host_dir.mkdir(parents=True)
        recording = Recording(
            id="pre-phase-8",
            request=Request(
                method="GET",
                url="http://api.example.com/v1/widgets",
                host="api.example.com",
                path="/v1/widgets",
            ),
            response=Response(status_code=200, body="ok"),
            metadata=RecordingMetadata(recorded_at=datetime.now(timezone.utc), source="live"),
        )
        payload = recording.model_dump_json(indent=2)
        import json as _json

        raw = _json.loads(payload)
        del raw["response"]["is_stream"]
        (host_dir / "pre-phase-8.json").write_text(_json.dumps(raw), encoding="utf-8")

        recorder = Recorder(tmp_path)
        loaded = recorder.load_all()
        assert len(loaded) == 1
        assert loaded[0].response.is_stream is False


# --------------------------------------------------------------------------
# Config: capture_streams / stream_delay (D10)
# --------------------------------------------------------------------------


class TestStreamingConfigFields:
    def test_defaults(self):
        config = Config()
        assert config.capture_streams is True
        assert config.stream_delay == 0.0

    def test_capture_streams_can_be_disabled(self):
        config = Config(capture_streams=False)
        assert config.capture_streams is False

    def test_stream_delay_accepts_positive_value(self):
        config = Config(stream_delay=0.5)
        assert config.stream_delay == 0.5

    def test_stream_delay_rejects_negative_value(self):
        with pytest.raises(ValueError, match="stream_delay must be >= 0"):
            Config(stream_delay=-0.1)
