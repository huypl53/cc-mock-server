"""RED-first tests for cc_mock_server.models.

Covers the full model surface defined up-front per plan.md D9: Request,
Response, RecordingMetadata, Recording, HandlerResult, PendingRequest, and
the body encode/decode helpers used to satisfy D8 (content-type awareness).
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from cc_mock_server.models import (
    HandlerResult,
    PendingRequest,
    Recording,
    RecordingMetadata,
    Request,
    Response,
    decode_body,
    encode_body,
    is_text_content_type,
)


class TestRequestModel:
    def test_minimal_construction_has_defaults(self):
        req = Request(method="GET", url="http://example.com/x", host="example.com", path="/x")

        assert req.query == {}
        assert req.headers == {}
        assert req.body == ""
        assert req.is_json is False
        assert req.content_type is None

    def test_full_construction_round_trips_via_json(self):
        req = Request(
            method="POST",
            url="http://example.com/v1/chat",
            host="example.com",
            path="/v1/chat",
            query={"stream": "false"},
            headers={"content-type": "application/json"},
            body='{"a": 1}',
            is_json=True,
            content_type="application/json",
        )
        dumped = req.model_dump_json()
        restored = Request.model_validate_json(dumped)
        assert restored == req

    def test_from_raw_json_body_sets_is_json_true(self):
        req = Request.from_raw(
            method="POST",
            url="http://example.com/v1/chat",
            host="example.com",
            path="/v1/chat",
            raw_body=b'{"hello": "world"}',
            content_type="application/json",
        )
        assert req.is_json is True
        assert req.content_type == "application/json"
        assert req.body == '{"hello": "world"}'
        assert req.decoded_body() == b'{"hello": "world"}'

    def test_from_raw_plain_text_body_is_not_json(self):
        req = Request.from_raw(
            method="POST",
            url="http://example.com/x",
            host="example.com",
            path="/x",
            raw_body=b"just some text",
            content_type="text/plain",
        )
        assert req.is_json is False
        assert req.body == "just some text"
        assert req.decoded_body() == b"just some text"

    def test_from_raw_binary_body_is_base64_encoded(self):
        raw = b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff\xfe"
        req = Request.from_raw(
            method="POST",
            url="http://example.com/upload",
            host="example.com",
            path="/upload",
            raw_body=raw,
            content_type="image/png",
        )

        assert req.is_json is False
        assert req.content_type == "image/png"
        # The body on the model must be the base64 text, not raw bytes.
        assert base64.b64decode(req.body) == raw
        # decoded_body() must invert the encoding losslessly.
        assert req.decoded_body() == raw

    def test_from_raw_empty_body(self):
        req = Request.from_raw(
            method="GET",
            url="http://example.com/x",
            host="example.com",
            path="/x",
            raw_body=b"",
            content_type=None,
        )
        assert req.body == ""
        assert req.is_json is False
        assert req.decoded_body() == b""


class TestResponseModel:
    def test_minimal_construction_has_defaults(self):
        resp = Response(status_code=200)
        assert resp.headers == {}
        assert resp.body == ""
        assert resp.is_json is False
        assert resp.content_type is None

    def test_from_raw_json_round_trip(self):
        resp = Response.from_raw(
            status_code=201,
            headers={"content-type": "application/json"},
            raw_body=b'{"ok": true}',
            content_type="application/json",
        )
        assert resp.is_json is True
        assert resp.decoded_body() == b'{"ok": true}'

    def test_from_raw_binary_round_trip(self):
        raw = bytes(range(256))
        resp = Response.from_raw(
            status_code=200,
            raw_body=raw,
            content_type="application/octet-stream",
        )
        assert resp.is_json is False
        assert resp.decoded_body() == raw


class TestBodyCodecHelpers:
    @pytest.mark.parametrize(
        "content_type,expected",
        [
            (None, True),
            ("", True),
            ("text/plain", True),
            ("text/html; charset=utf-8", True),
            ("application/json", True),
            ("application/json; charset=utf-8", True),
            ("application/xml", True),
            ("application/x-www-form-urlencoded", True),
            ("image/png", False),
            ("application/octet-stream", False),
            ("multipart/form-data; boundary=x", False),
            ("application/gzip", False),
        ],
    )
    def test_is_text_content_type(self, content_type, expected):
        assert is_text_content_type(content_type) is expected

    def test_encode_decode_empty_body(self):
        body, is_json = encode_body(b"", "application/json")
        assert body == ""
        assert is_json is False
        assert decode_body(body, "application/json") == b""

    def test_encode_decode_non_json_text_body(self):
        raw = b"a=1&b=2"
        body, is_json = encode_body(raw, "application/x-www-form-urlencoded")
        assert is_json is False
        assert body == "a=1&b=2"
        assert decode_body(body, "application/x-www-form-urlencoded") == raw

    def test_encode_rejects_malformed_json_text_as_non_json(self):
        raw = b"not-json-but-text-content-type"
        body, is_json = encode_body(raw, "application/json")
        assert is_json is False
        assert body == "not-json-but-text-content-type"


class TestRecordingMetadata:
    def test_fuzzy_key_defaults_to_none(self):
        meta = RecordingMetadata(recorded_at=datetime.now(timezone.utc), source="live")
        assert meta.fuzzy_key is None

    def test_fuzzy_key_accepts_string(self):
        meta = RecordingMetadata(recorded_at=datetime.now(timezone.utc), source="live", fuzzy_key="abc123")
        assert meta.fuzzy_key == "abc123"


class TestRecording:
    def test_construction_and_json_round_trip(self):
        req = Request(method="GET", url="http://example.com/x", host="example.com", path="/x")
        resp = Response(status_code=200, body="ok")
        meta = RecordingMetadata(recorded_at=datetime.now(timezone.utc), source="live", fuzzy_key=None)
        recording = Recording(id="GET_x_20260101_abcd1234", request=req, response=resp, metadata=meta)

        restored = Recording.model_validate_json(recording.model_dump_json())
        assert restored.id == recording.id
        assert restored.request == req
        assert restored.response == resp
        assert restored.metadata.fuzzy_key is None


class TestHandlerResult:
    def test_respond_requires_response(self):
        with pytest.raises(ValidationError):
            HandlerResult(action="respond", response=None)

    def test_respond_with_response_ok(self):
        resp = Response(status_code=200)
        result = HandlerResult(action="respond", response=resp)
        assert result.action == "respond"
        assert result.response is resp or result.response == resp

    def test_pass_through_allows_no_response(self):
        result = HandlerResult(action="pass_through", response=None)
        assert result.action == "pass_through"
        assert result.response is None

    def test_invalid_action_rejected(self):
        with pytest.raises(ValidationError):
            HandlerResult(action="bogus", response=None)


class TestPendingRequest:
    @pytest.mark.asyncio
    async def test_holds_a_real_future_and_resolves(self):
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        req = Request(method="GET", url="http://example.com/x", host="example.com", path="/x")
        pending = PendingRequest(
            request_id="abc-123",
            request=req,
            future=fut,
            created_at=datetime.now(timezone.utc),
        )

        assert pending.request_id == "abc-123"
        assert pending.future is fut

        resp = Response(status_code=200, body="hi")
        pending.future.set_result(resp)
        result = await pending.future
        assert result is resp
