"""RED-first tests for cc_mock_server.recorder and cc_mock_server.sanitize.

Covers every phase-02 success criterion: host/path traversal confinement
(H3), filename length cap, sensitive-header masking on disk (D3), binary
body base64 round-trip (D8), stable filename-stem ids (H4), corrupt-file
skip on load, and concurrent async save/delete under the D6 lock.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cc_mock_server.models import Recording, RecordingMetadata, Request, Response
from cc_mock_server.recorder import Recorder
from cc_mock_server.sanitize import (
    DEFAULT_SENSITIVE_HEADERS,
    MASK_VALUE,
    build_filename,
    confine_path,
    mask_headers,
    sanitize_host,
    slugify_path,
)


def make_request(**overrides) -> Request:
    defaults = dict(
        method="POST",
        url="http://example.com/v1/chat/completions",
        host="example.com",
        path="/v1/chat/completions",
        query={},
        headers={"content-type": "application/json"},
        body='{"hello": "world"}',
        is_json=True,
        content_type="application/json",
    )
    defaults.update(overrides)
    return Request(**defaults)


def make_response(**overrides) -> Response:
    defaults = dict(
        status_code=200,
        headers={"content-type": "application/json"},
        body='{"ok": true}',
        is_json=True,
        content_type="application/json",
    )
    defaults.update(overrides)
    return Response(**defaults)


# --------------------------------------------------------------------------
# sanitize.py: pure helper unit tests
# --------------------------------------------------------------------------


class TestMaskHeaders:
    def test_default_sensitive_headers_masked_case_insensitive(self):
        headers = {
            "Authorization": "Bearer secret-token",
            "X-Api-Key": "key-123",
            "api-key": "another-key",
            "Cookie": "session=abc",
            "Set-Cookie": "session=abc; Path=/",
            "Content-Type": "application/json",
        }
        masked = mask_headers(headers, DEFAULT_SENSITIVE_HEADERS)

        assert masked["Authorization"] == MASK_VALUE
        assert masked["X-Api-Key"] == MASK_VALUE
        assert masked["api-key"] == MASK_VALUE
        assert masked["Cookie"] == MASK_VALUE
        assert masked["Set-Cookie"] == MASK_VALUE
        assert masked["Content-Type"] == "application/json"

    def test_mask_headers_does_not_mutate_input(self):
        headers = {"Authorization": "secret"}
        mask_headers(headers, DEFAULT_SENSITIVE_HEADERS)
        assert headers["Authorization"] == "secret"

    def test_mask_headers_empty(self):
        assert mask_headers({}, DEFAULT_SENSITIVE_HEADERS) == {}


class TestSanitizeHost:
    def test_plain_host_preserved(self):
        assert sanitize_host("example.com") == "example.com"

    def test_traversal_sequence_neutralized_no_separators_remain(self):
        result = sanitize_host("../../x")
        assert "/" not in result
        assert "\\" not in result
        assert result not in ("..", ".", "")

    def test_exact_dotdot_falls_back_to_safe_default(self):
        result = sanitize_host("..")
        assert result == "unknown-host"

    def test_backslash_traversal_neutralized(self):
        result = sanitize_host("..\\..\\x")
        assert "/" not in result
        assert "\\" not in result

    def test_empty_host_falls_back(self):
        assert sanitize_host("") == "unknown-host"


class TestSlugifyPath:
    def test_strips_query_and_slashes(self):
        slug = slugify_path("/v1/chat/completions")
        assert "/" not in slug
        assert slug

    def test_empty_path_yields_root(self):
        assert slugify_path("/") == "root"
        assert slugify_path("") == "root"


class TestBuildFilename:
    def test_short_name_not_truncated(self):
        name = build_filename("POST", "v1_chat", "20260101T000000000000", "abcd1234")
        assert name == "POST_v1_chat_20260101T000000000000_abcd1234.json"

    def test_long_slug_capped_at_255_bytes_and_keeps_unique_suffix(self):
        long_slug = "x" * 1000
        name = build_filename("POST", long_slug, "20260101T000000000000", "abcd1234")
        assert len(name.encode("utf-8")) <= 255
        assert name.endswith("_20260101T000000000000_abcd1234.json")


class TestConfinePath:
    def test_confines_normal_subpath(self, tmp_path: Path):
        result = confine_path(tmp_path, "host", "file.json")
        assert result == (tmp_path / "host" / "file.json").resolve()

    def test_rejects_escape_via_dotdot(self, tmp_path: Path):
        with pytest.raises(ValueError):
            confine_path(tmp_path, "..", "..", "etc", "passwd")


# --------------------------------------------------------------------------
# Recorder: integration tests
# --------------------------------------------------------------------------


class TestRecorderSave:
    @pytest.mark.asyncio
    async def test_save_creates_host_dir_and_json_file(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        recording = await recorder.save(make_request(), make_response())

        host_dir = tmp_path / "example.com"
        assert host_dir.is_dir()
        files = list(host_dir.glob("*.json"))
        assert len(files) == 1
        assert files[0].stem == recording.id

    @pytest.mark.asyncio
    async def test_id_equals_filename_stem_and_is_stable(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        recording = await recorder.save(make_request(), make_response())

        # Reload from disk into a fresh Recorder instance; id must match.
        reloaded_recorder = Recorder(tmp_path)
        loaded = reloaded_recorder.load_all()
        assert len(loaded) == 1
        assert loaded[0].id == recording.id

    @pytest.mark.asyncio
    async def test_round_trip_preserves_json_body(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        req = make_request(body='{"a": [1, 2, 3], "b": "text"}')
        resp = make_response(body='{"ok": true, "n": 42}')
        await recorder.save(req, resp)

        reloaded = Recorder(tmp_path)
        loaded = reloaded.load_all()
        assert len(loaded) == 1
        assert loaded[0].request.decoded_body() == req.decoded_body()
        assert loaded[0].response.decoded_body() == resp.decoded_body()
        assert loaded[0].request.is_json is True
        assert loaded[0].response.is_json is True

    @pytest.mark.asyncio
    async def test_binary_body_round_trips_via_base64(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        raw = bytes(range(256))
        req = Request.from_raw(
            method="POST",
            url="http://example.com/upload",
            host="example.com",
            path="/upload",
            raw_body=raw,
            content_type="application/octet-stream",
        )
        resp = make_response()
        recording = await recorder.save(req, resp)

        assert recording.request.is_json is False
        # on-disk body must be base64 text, not raw bytes
        assert base64.b64decode(recording.request.body) == raw

        reloaded = Recorder(tmp_path)
        loaded = reloaded.load_all()
        assert loaded[0].request.decoded_body() == raw

    @pytest.mark.asyncio
    async def test_sensitive_headers_masked_on_disk(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        req = make_request(
            headers={
                "Authorization": "Bearer super-secret-token",
                "X-Api-Key": "sk-super-secret",
                "Cookie": "session=super-secret-cookie",
                "Content-Type": "application/json",
            }
        )
        recording = await recorder.save(req, make_response())

        raw_files = list(tmp_path.rglob("*.json"))
        assert len(raw_files) == 1
        on_disk_text = raw_files[0].read_text(encoding="utf-8")

        assert "super-secret-token" not in on_disk_text
        assert "sk-super-secret" not in on_disk_text
        assert "super-secret-cookie" not in on_disk_text
        assert MASK_VALUE in on_disk_text

        # in-memory recording (returned from save) must also be masked —
        # the raw secret must never be retained anywhere after save().
        assert recording.request.headers["Authorization"] == MASK_VALUE
        assert recording.request.headers["X-Api-Key"] == MASK_VALUE
        assert recording.request.headers["Cookie"] == MASK_VALUE
        assert recording.request.headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_slug_safe_for_path_with_slashes_and_query(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        req = make_request(
            url="http://example.com/v1/chat/completions?stream=true&foo=bar",
            path="/v1/chat/completions",
            query={"stream": "true", "foo": "bar"},
        )
        recording = await recorder.save(req, make_response())

        host_dir = tmp_path / "example.com"
        files = list(host_dir.glob("*.json"))
        assert len(files) == 1
        filename = files[0].name
        assert "?" not in filename
        assert "/" not in filename
        assert recording.id == files[0].stem

    @pytest.mark.asyncio
    async def test_host_with_traversal_sequence_cannot_escape_recordings_dir(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        req = make_request(host="../../../../etc")
        await recorder.save(req, make_response())

        # No file must land outside tmp_path.
        all_files = list(tmp_path.rglob("*.json"))
        assert len(all_files) == 1
        for f in all_files:
            f.resolve().relative_to(tmp_path.resolve())

        # And nothing escaped upward from tmp_path's parent either.
        parent_escape = tmp_path.parent / "etc"
        assert not parent_escape.exists()

    @pytest.mark.asyncio
    async def test_long_path_produces_filename_capped_at_255_bytes(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        req = make_request(path="/" + ("segment" * 100))
        await recorder.save(req, make_response())

        host_dir = tmp_path / "example.com"
        files = list(host_dir.glob("*.json"))
        assert len(files) == 1
        assert len(files[0].name.encode("utf-8")) <= 255


class TestRecorderLoadAll:
    def test_load_all_on_empty_dir_returns_empty_list(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        assert recorder.load_all() == []

    def test_load_all_skips_corrupt_file_and_warns(self, tmp_path: Path, caplog):
        host_dir = tmp_path / "example.com"
        host_dir.mkdir(parents=True)
        (host_dir / "corrupt.json").write_text("{not valid json!!", encoding="utf-8")

        recorder = Recorder(tmp_path)
        with caplog.at_level(logging.WARNING):
            result = recorder.load_all()

        assert result == []
        assert any("corrupt.json" in message for message in caplog.messages) or caplog.records

    def test_load_all_skips_corrupt_but_keeps_valid_siblings(self, tmp_path: Path):
        host_dir = tmp_path / "example.com"
        host_dir.mkdir(parents=True)
        (host_dir / "corrupt.json").write_text("not json", encoding="utf-8")

        good = Recording(
            id="GOOD_id_1",
            request=make_request(),
            response=make_response(),
            metadata=RecordingMetadata(recorded_at=datetime.now(timezone.utc), source="live"),
        )
        (host_dir / "GOOD_id_1.json").write_text(good.model_dump_json(), encoding="utf-8")

        recorder = Recorder(tmp_path)
        result = recorder.load_all()
        assert len(result) == 1
        assert result[0].id == "GOOD_id_1"


class TestRecorderDeleteAndList:
    @pytest.mark.asyncio
    async def test_delete_removes_file_and_from_list(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        recording = await recorder.save(make_request(), make_response())

        deleted = await recorder.delete(recording.id)
        assert deleted is True
        assert recorder.list() == []
        assert list(tmp_path.rglob("*.json")) == []

    @pytest.mark.asyncio
    async def test_delete_unknown_id_returns_false(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        assert await recorder.delete("does-not-exist") is False

    @pytest.mark.asyncio
    async def test_list_returns_all_saved_recordings(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        await recorder.save(make_request(), make_response())
        await recorder.save(make_request(path="/other"), make_response())

        assert len(recorder.list()) == 2

    @pytest.mark.asyncio
    async def test_snapshot_is_independent_of_later_mutation(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        await recorder.save(make_request(), make_response())

        snapshot = recorder.snapshot()
        assert len(snapshot) == 1

        await recorder.save(make_request(path="/other"), make_response())
        # the earlier snapshot must not grow
        assert len(snapshot) == 1
        assert len(recorder.snapshot()) == 2


class TestRecorderFuzzyKey:
    @pytest.mark.asyncio
    async def test_fuzzy_key_is_none_on_save(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        recording = await recorder.save(make_request(), make_response())
        assert recording.metadata.fuzzy_key is None


class TestRecorderConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_save_and_delete_no_error(self, tmp_path: Path):
        recorder = Recorder(tmp_path)

        async def save_one(i: int) -> Recording:
            return await recorder.save(make_request(path=f"/path-{i}"), make_response())

        recordings = await asyncio.gather(*(save_one(i) for i in range(20)))
        assert len(recordings) == 20
        assert len(recorder.list()) == 20

        async def delete_one(rid: str) -> bool:
            return await recorder.delete(rid)

        results = await asyncio.gather(*(delete_one(r.id) for r in recordings[:10]))
        assert all(results)
        assert len(recorder.list()) == 10

    @pytest.mark.asyncio
    async def test_interleaved_save_and_delete_no_exception(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        first = await recorder.save(make_request(), make_response())

        async def deleter():
            await recorder.delete(first.id)

        async def saver(i: int):
            await recorder.save(make_request(path=f"/p{i}"), make_response())

        await asyncio.gather(deleter(), *(saver(i) for i in range(10)))
        # first was deleted, 10 new ones saved -> 10 remain
        assert len(recorder.list()) == 10
