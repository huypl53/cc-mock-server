"""Integration tests: a real mitmproxy `Master` (via `app.build_application`)
fronting `HTTP_PROXY`-style traffic (plan.md phase 6, Implementation Step 2).

Every test builds a fresh `Application` on an ephemeral proxy port
(`config.proxy_port=0`), discovers the actually-bound port through
mitmproxy's own `proxyserver` addon (`servers.listen_addrs()` -- no
port-guessing/race), and tears the `Master` down deterministically in a
`finally` so no per-test event-loop/socket state leaks into the next test.

`upstream_cert` is disabled (`app.master.options.upstream_cert = False`) so
HTTPS tests never need real network reachability to the "target" host --
mitmproxy is happy to mint a self-signed interception certificate for any
SNI/CONNECT-target without first dialing out. A tiny local
`asyncio.start_server` stands in as the "real" upstream so the CONNECT
handshake has something loopback-reachable to tunnel to; our addon answers
every intercepted flow itself (`flow.response` set before mitmproxy would
ever actually relay to that dummy upstream), so the dummy never needs to
speak real HTTP/TLS back.

httpx 0.28 renamed `Client(proxies=...)` to `Client(proxy=...)` (singular) --
this suite uses the installed 0.28.1 API, not the older `proxies=` kwarg
mentioned in some historical mitmproxy docs.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
from pytest_httpserver import HTTPServer

from cc_mock_server.app import Application, build_application, run_proxy, shutdown
from cc_mock_server.config import Config
from cc_mock_server.enums import FilterMode, Mode

_READY_TIMEOUT = 10.0
_TEARDOWN_TIMEOUT = 5.0


async def _wait_for_listen_port(app: Application, timeout: float = _READY_TIMEOUT) -> int:
    """Poll mitmproxy's own `proxyserver` addon for the actually-bound port
    (config uses `proxy_port=0`/ephemeral) instead of guessing a free port
    ahead of time and racing another process for it."""
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
    config: Config, tmp_path: Path, *, upstream_cert: bool = False
) -> AsyncIterator[tuple[Application, int, Path]]:
    """Build + run a real mitmproxy `Master` for the duration of the `with`
    block, then tear it down deterministically (D1: single event loop --
    everything here runs on the current, already-running, test loop)."""
    confdir = tmp_path / "confdir"
    app = build_application(config, confdir=confdir)
    app.master.options.upstream_cert = upstream_cert

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


@asynccontextmanager
async def dummy_tcp_upstream() -> AsyncIterator[tuple[int, bytearray]]:
    """A minimal loopback TCP listener that just accepts + captures the
    first chunk of bytes it receives, then closes. Stands in as the "real"
    upstream host for HTTPS CONNECT targets -- never needs to speak
    HTTP/TLS back because our addon always answers intercepted flows
    itself before mitmproxy would relay anything to it."""
    captured = bytearray()

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await reader.read(65536)
            captured.extend(data)
        finally:
            writer.close()

    server = await asyncio.start_server(_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port, captured
    finally:
        server.close()
        await server.wait_closed()


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
# HTTP: intercept -> agent response -> record -> switch to replay -> same
# request served from the recording without calling the agent again.
# --------------------------------------------------------------------------


class TestHttpRecordThenReplay:
    @pytest.mark.asyncio
    async def test_http_intercept_record_then_replay_without_agent(
        self, tmp_path: Path, httpserver: HTTPServer
    ):
        httpserver.expect_request("/agent", method="POST").respond_with_json(
            {"status_code": 200, "body": {"served": "agent-1"}}
        )
        config = make_config(
            tmp_path,
            mode=Mode.LIVE,
            agent_mode="sync",
            agent_url=httpserver.url_for("/agent"),
        )

        async with running_proxy(config, tmp_path) as (app, port, _confdir):
            async with httpx.AsyncClient(
                proxy=f"http://127.0.0.1:{port}", verify=False, timeout=5.0
            ) as client:
                first = await client.post(
                    "http://mock-target.test/v1/widgets", json={"id": 1}
                )
                assert first.status_code == 200
                assert first.json() == {"served": "agent-1"}

                recordings = _recording_files(config.recordings_dir)
                assert len(recordings) == 1
                assert len(httpserver.log) == 1

                # Switch to replay mode on the SAME shared Config instance
                # (D6: the composition root is the single owner of state).
                app.config.mode = Mode.REPLAY

                second = await client.post(
                    "http://mock-target.test/v1/widgets", json={"id": 1}
                )
                assert second.status_code == 200
                assert second.json() == {"served": "agent-1"}

                # replay must NOT have called the agent again
                assert len(httpserver.log) == 1
                # and must not have written a second recording
                assert len(_recording_files(config.recordings_dir)) == 1


# --------------------------------------------------------------------------
# HTTPS (D7, Global AC #11): intercept -> record -> replay, all over TLS
# terminated with the mitmproxy-generated CA.
# --------------------------------------------------------------------------


class TestHttpsRecordThenReplay:
    @pytest.mark.asyncio
    async def test_https_intercept_record_then_replay_over_tls(
        self, tmp_path: Path, httpserver: HTTPServer
    ):
        httpserver.expect_request("/agent", method="POST").respond_with_json(
            {"status_code": 200, "body": {"served": "agent-https"}}
        )
        config = make_config(
            tmp_path,
            mode=Mode.LIVE,
            agent_mode="sync",
            agent_url=httpserver.url_for("/agent"),
        )

        async with dummy_tcp_upstream() as (upstream_port, _captured):
            async with running_proxy(config, tmp_path) as (app, port, confdir):
                ca_path = confdir / "mitmproxy-ca-cert.pem"
                for _ in range(100):
                    if ca_path.exists():
                        break
                    await asyncio.sleep(0.05)
                assert ca_path.exists(), "mitmproxy did not generate a CA cert in time"
                ssl_context = ssl.create_default_context(cafile=str(ca_path))

                async with httpx.AsyncClient(
                    proxy=f"http://127.0.0.1:{port}", verify=ssl_context, timeout=5.0
                ) as client:
                    url = f"https://127.0.0.1:{upstream_port}/v1/secrets"

                    first = await client.post(url, json={"id": 42})
                    assert first.status_code == 200
                    assert first.json() == {"served": "agent-https"}

                    recordings = _recording_files(config.recordings_dir)
                    assert len(recordings) == 1
                    assert len(httpserver.log) == 1

                    app.config.mode = Mode.REPLAY

                    second = await client.post(url, json={"id": 42})
                    assert second.status_code == 200
                    assert second.json() == {"served": "agent-https"}

                    assert len(httpserver.log) == 1
                    assert len(_recording_files(config.recordings_dir)) == 1


# --------------------------------------------------------------------------
# domain outside the filter -> pass-through, no decrypt, no record (D7).
# --------------------------------------------------------------------------


class TestDomainFilterPassThrough:
    @pytest.mark.asyncio
    async def test_out_of_filter_domain_is_never_decrypted_or_recorded(self, tmp_path: Path):
        config = make_config(
            tmp_path,
            mode=Mode.LIVE,
            filter_mode=FilterMode.WHITELIST,
            filter_domains=["only-allowed.test"],  # does NOT match 127.0.0.1
        )

        async with dummy_tcp_upstream() as (upstream_port, captured):
            async with running_proxy(config, tmp_path) as (_app, port, _confdir):
                async with httpx.AsyncClient(
                    proxy=f"http://127.0.0.1:{port}", verify=False, timeout=3.0
                ) as client:
                    with pytest.raises(httpx.HTTPError):
                        # the dummy upstream doesn't speak real TLS, so the
                        # handshake itself fails -- that failure is exactly
                        # the point: mitmproxy tunneled the raw bytes
                        # through unmodified instead of terminating TLS.
                        await client.post(
                            f"https://127.0.0.1:{upstream_port}/secret", json={"a": 1}
                        )

                # give the raw tunnel a moment to actually deliver bytes
                for _ in range(50):
                    if captured:
                        break
                    await asyncio.sleep(0.05)

                # A real (unmodified, un-decrypted) TLS ClientHello record
                # starts with 0x16 0x03 (handshake, TLS 1.x) -- proof
                # mitmproxy never terminated TLS for this out-of-filter host.
                assert bytes(captured[:2]) == b"\x16\x03"
                assert _recording_files(config.recordings_dir) == []
