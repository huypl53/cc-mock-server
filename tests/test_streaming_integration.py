"""Integration tests: SSE capture-on-pass-through + replay through a REAL
mitmproxy `Master` (plan.md phase 8, D10).

Mirrors `tests/test_proxy_integration.py`'s pattern exactly (ephemeral
proxy port discovered via mitmproxy's own `proxyserver` addon, readiness
polling, deterministic per-test teardown) -- duplicated locally rather than
imported, since that module belongs to phase 6 and isn't in this phase's
file-ownership list.

The "real SSE origin" is a tiny hand-rolled `asyncio.start_server` that
speaks just enough HTTP/1.1 (chunked transfer-encoding, so a real proxy
sees genuine incremental body frames rather than a close-delimited body)
to emit `data: {...}\n\n` events followed by a `data: [DONE]\n\n` sentinel.
`pytest_httpserver` (werkzeug/WSGI) is not used for this because WSGI
buffers full bodies rather than trickling chunks, which is exactly the TTFT
behavior D10 is testing.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx
import pytest

from cc_mock_server.app import Application, build_application, run_proxy, shutdown
from cc_mock_server.config import Config
from cc_mock_server.enums import FilterMode, Mode
from cc_mock_server.streaming import parse_sse_events

_READY_TIMEOUT = 10.0
_TEARDOWN_TIMEOUT = 5.0

SSE_EVENTS = ['data: {"delta": "hello"}', 'data: {"delta": "world"}', "data: [DONE]"]


# --------------------------------------------------------------------------
# proxy lifecycle helpers (duplicated from test_proxy_integration.py -- see
# module docstring for why)
# --------------------------------------------------------------------------


async def _wait_for_listen_port(app: Application, timeout: float = _READY_TIMEOUT) -> int:
    proxyserver = app.master.addons.get("proxyserver")
    assert proxyserver is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        addrs = proxyserver.listen_addrs()
        if addrs:
            return addrs[0][1]
        await asyncio.sleep(0.02)
    raise TimeoutError("mitmproxy proxyserver did not start listening in time")


@asynccontextmanager
async def running_proxy(
    config: Config, tmp_path: Path
) -> AsyncIterator[tuple[Application, int, Path]]:
    confdir = tmp_path / "confdir"
    app = build_application(config, confdir=confdir)
    app.master.options.upstream_cert = False

    task = asyncio.create_task(run_proxy(app))
    try:
        port = await _wait_for_listen_port(app)
        yield app, port, confdir
    finally:
        await shutdown(app)
        try:
            await asyncio.wait_for(task, timeout=_TEARDOWN_TIMEOUT)
        except asyncio.TimeoutError:  # pragma: no cover -- defensive, shouldn't happen
            task.cancel()


def make_config(tmp_path: Path, **overrides) -> Config:
    defaults: dict = dict(
        proxy_port=0,
        recordings_dir=tmp_path / "recordings",
        filter_mode=FilterMode.WHITELIST,
        filter_domains=["*"],
        agent_timeout=0.3,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _recording_files(recordings_dir: Path) -> list[Path]:
    return sorted(recordings_dir.rglob("*.json"))


# --------------------------------------------------------------------------
# a real (if minimal) chunked SSE origin server
# --------------------------------------------------------------------------


class SSEOrigin:
    """A loopback HTTP/1.1 server that streams `events` as SSE using real
    chunked transfer-encoding (so mitmproxy sees genuine incremental
    frames, not a single buffered body). `hits` counts completed request
    handshakes -- used to prove replay never re-contacts this origin."""

    def __init__(self, events: list[str], *, chunk_delay: float = 0.0) -> None:
        self.events = events
        self.chunk_delay = chunk_delay
        self.hits = 0
        self._server: Optional[asyncio.base_events.Server] = None
        self.port = 0

    async def start(self) -> "SSEOrigin":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # Drain the request line + headers (terminated by a blank line).
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b""):
                    break
            self.hits += 1
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
            )
            await writer.drain()
            for event in self.events:
                payload = (event + "\n\n").encode("utf-8")
                writer.write(f"{len(payload):x}\r\n".encode("ascii") + payload + b"\r\n")
                await writer.drain()
                if self.chunk_delay:
                    await asyncio.sleep(self.chunk_delay)
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()


@asynccontextmanager
async def sse_origin(events: list[str], *, chunk_delay: float = 0.0) -> AsyncIterator[SSEOrigin]:
    origin = await SSEOrigin(events, chunk_delay=chunk_delay).start()
    try:
        yield origin
    finally:
        await origin.stop()


# --------------------------------------------------------------------------
# pass-through capture: real SSE origin -> tee -> recording (is_stream=True)
# --------------------------------------------------------------------------


class TestPassThroughCaptureAndReplay:
    @pytest.mark.asyncio
    async def test_capture_on_pass_through_records_raw_sse_body(self, tmp_path: Path):
        config = make_config(tmp_path, mode=Mode.LIVE)

        async with sse_origin(SSE_EVENTS) as origin:
            async with running_proxy(config, tmp_path) as (app, port, _confdir):
                # No agent_url is configured and capture_streams defaults
                # True -- but auto_select_filtered defaults True too (every
                # in-filter host is "selected" by default), which would
                # route this to the (non-existent) agent. Deselect
                # everything so this exercises the actual D10 scenario:
                # live mode, unselected endpoint -> falls through to the
                # replay-miss path -> default `PASS_THROUGH` strategy ->
                # mitmproxy really contacts the origin -> tee-capture.
                await app.selector.select_none()

                async with httpx.AsyncClient(
                    proxy=f"http://127.0.0.1:{port}", verify=False, timeout=5.0
                ) as client:
                    url = f"http://127.0.0.1:{origin.port}/v1/stream"
                    async with client.stream("GET", url) as resp:
                        assert resp.status_code == 200
                        assert resp.headers["content-type"] == "text/event-stream"
                        body = (await resp.aread()).decode("utf-8")

                assert parse_sse_events(body) == SSE_EVENTS
                assert origin.hits == 1

        recordings = _recording_files(config.recordings_dir)
        assert len(recordings) == 1
        import json

        raw = json.loads(recordings[0].read_text(encoding="utf-8"))
        assert raw["response"]["is_stream"] is True
        assert raw["response"]["content_type"] == "text/event-stream"
        assert parse_sse_events(raw["response"]["body"]) == SSE_EVENTS
        # framing headers must NOT have been captured verbatim (they'd be
        # stale/wrong once replayed as a single non-chunked body).
        assert "transfer-encoding" not in {k.lower() for k in raw["response"]["headers"]}

    @pytest.mark.asyncio
    async def test_replay_reemits_sse_without_touching_origin(self, tmp_path: Path):
        config = make_config(tmp_path, mode=Mode.LIVE)

        async with sse_origin(SSE_EVENTS) as origin:
            async with running_proxy(config, tmp_path) as (app, port, _confdir):
                await app.selector.select_none()
                url = f"http://127.0.0.1:{origin.port}/v1/stream"

                async with httpx.AsyncClient(
                    proxy=f"http://127.0.0.1:{port}", verify=False, timeout=5.0
                ) as client:
                    first = await client.get(url)
                    assert first.status_code == 200
                    assert parse_sse_events(first.text) == SSE_EVENTS
                    assert origin.hits == 1

                    # Prove replay doesn't need the origin at all: shut it
                    # down, then switch to replay mode and repeat the exact
                    # same request.
                    await origin.stop()
                    app.config.mode = Mode.REPLAY

                    second = await client.get(url)
                    assert second.status_code == 200
                    assert second.headers["content-type"] == "text/event-stream"
                    assert parse_sse_events(second.text) == SSE_EVENTS

                # origin was stopped before the replay request -- if
                # anything had tried to actually dial it, the request
                # would have failed outright (connection refused) rather
                # than succeeding with the right content.
                assert origin.hits == 1
                assert len(_recording_files(config.recordings_dir)) == 1


# --------------------------------------------------------------------------
# stream_delay: documented content-correct fallback (see server.py's module
# docstring for the exact mitmproxy blocker) -- never fake timing.
# --------------------------------------------------------------------------


class TestStreamDelayIsContentCorrectFallback:
    @pytest.mark.asyncio
    async def test_stream_delay_configured_replay_is_still_content_correct_and_fast(
        self, tmp_path: Path
    ):
        """`stream_delay > 0` cannot pace a replayed (injected) response --
        see server.py's module docstring for the source-verified mitmproxy
        blocker (`send_response(already_streamed=False)` always sends the
        full body as a single chunk, before any bytes reach the client, no
        matter what `flow.response.stream` is set to). This asserts the
        HONEST behavior: content is correct, AND replay is fast (proving no
        artificial/fake per-event delay was smuggled in elsewhere)."""
        config = make_config(tmp_path, mode=Mode.LIVE, stream_delay=0.5)

        async with sse_origin(SSE_EVENTS) as origin:
            async with running_proxy(config, tmp_path) as (app, port, _confdir):
                await app.selector.select_none()
                url = f"http://127.0.0.1:{origin.port}/v1/stream"

                async with httpx.AsyncClient(
                    proxy=f"http://127.0.0.1:{port}", verify=False, timeout=5.0
                ) as client:
                    first = await client.get(url)
                    assert parse_sse_events(first.text) == SSE_EVENTS

                    await origin.stop()
                    app.config.mode = Mode.REPLAY

                    start = time.monotonic()
                    second = await client.get(url)
                    elapsed = time.monotonic() - start

                assert parse_sse_events(second.text) == SSE_EVENTS
                # 3 events * 0.5s stream_delay would be >= 1.0s if pacing
                # were (fakily) applied; the real, documented behavior
                # emits the whole body in one frame, so this comfortably
                # clears well under that.
                assert elapsed < 1.0


# --------------------------------------------------------------------------
# client disconnect mid-stream -> no partial recording (D5 reused for D10)
# --------------------------------------------------------------------------


class TestClientDisconnectMidStream:
    @pytest.mark.asyncio
    async def test_disconnect_before_stream_completes_writes_no_recording(self, tmp_path: Path):
        config = make_config(tmp_path, mode=Mode.LIVE)

        # Generous delay between chunks so the test has a wide window to
        # disconnect after the first event but well before the origin
        # finishes sending.
        async with sse_origin(SSE_EVENTS, chunk_delay=0.3) as origin:
            async with running_proxy(config, tmp_path) as (app, port, _confdir):
                await app.selector.select_none()
                url = f"http://127.0.0.1:{origin.port}/v1/stream"

                client = httpx.AsyncClient(
                    proxy=f"http://127.0.0.1:{port}", verify=False, timeout=5.0
                )
                try:
                    async with client.stream("GET", url) as resp:
                        assert resp.status_code == 200
                        first_chunk = await resp.aiter_bytes().__anext__()
                        assert first_chunk  # got at least the first event
                        # Deliberately abandon the stream mid-way rather
                        # than reading to completion.
                finally:
                    await client.aclose()

                # Give the addon a brief moment to observe the disconnect
                # and clean up (`error`/`client_disconnected` hooks).
                for _ in range(50):
                    if _recording_files(config.recordings_dir):
                        break  # pragma: no cover -- would indicate a bug
                    await asyncio.sleep(0.05)

        assert _recording_files(config.recordings_dir) == []
