"""The cross-loop test (plan.md D1/C6, phase 7 Implementation Step 2) --
THE gate for this phase.

Boots the proxy (a real mitmproxy `Master` via `app.build_application`)
AND the control API (a real `uvicorn.Server`) on ONE event loop, via
`asyncio.gather(app.master.run(), server.serve())` -- exactly the pattern
`cli.py`'s `start` command uses in production. This is deliberately NOT an
isolated `TestClient`/ASGITransport loop (see `test_control_api.py` for
that flavor of test): the whole point is to prove that a mitmproxy flow
task blocked awaiting an `agent_mode=pending` future does not starve the
same loop's ability to also run the control API's `POST /mock/respond`
handler -- i.e. the single-loop architecture (D1) actually works
end-to-end, not just in theory.

Flow:
1. Start proxy + control API together on one loop.
2. Fire an app request through the proxy (`agent_mode=pending`, no
   `agent_url` -- resolution is only ever via `POST /mock/respond`). This
   blocks the request task on an `asyncio.Future`.
3. From a SEPARATE httpx client, poll `GET /mock/pending` until the
   request appears, then `POST /mock/respond`.
4. Assert the original (still-blocked) request unblocks with exactly that
   body, AND that a recording was written for it.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
import uvicorn

from cc_mock_server.app import Application, build_application, shutdown
from cc_mock_server.config import Config
from cc_mock_server.control_api import create_control_api
from cc_mock_server.enums import AgentMode, FilterMode, Mode

_READY_TIMEOUT = 10.0
_TEARDOWN_TIMEOUT = 5.0


def make_config(tmp_path: Path, **overrides) -> Config:
    defaults: dict = dict(
        proxy_port=0,
        control_port=0,
        control_bind="127.0.0.1",
        recordings_dir=tmp_path / "recordings",
        filter_mode=FilterMode.WHITELIST,
        filter_domains=["*"],
        mode=Mode.LIVE,
        agent_mode=AgentMode.PENDING,
        agent_timeout=5.0,
    )
    defaults.update(overrides)
    return Config(**defaults)


async def _wait_for_proxy_port(app: Application, timeout: float = _READY_TIMEOUT) -> int:
    """Poll mitmproxy's own `proxyserver` addon for the actually-bound port
    (config uses `proxy_port=0`) -- no port-guessing/race."""
    proxyserver = app.master.addons.get("proxyserver")
    assert proxyserver is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        addrs = proxyserver.listen_addrs()
        if addrs:
            return addrs[0][1]
        await asyncio.sleep(0.02)
    raise TimeoutError("mitmproxy proxyserver did not start listening in time")


async def _wait_for_control_port(server: "uvicorn.Server", timeout: float = _READY_TIMEOUT) -> int:
    """Poll uvicorn's internal state for the actually-bound port (config
    uses `control_port=0`), mirroring `_wait_for_proxy_port` above."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.started and server.servers:
            sockets = server.servers[0].sockets
            if sockets:
                return sockets[0].getsockname()[1]
        await asyncio.sleep(0.02)
    raise TimeoutError("uvicorn control API did not start listening in time")


@asynccontextmanager
async def running_app_with_control_api(
    config: Config, tmp_path: Path
) -> AsyncIterator[tuple[Application, int, int]]:
    """Build + run a REAL mitmproxy `Master` AND a REAL `uvicorn.Server` on
    the CURRENT (already running) event loop, sharing that one loop via a
    single `asyncio.gather(...)` -- exactly `cli.py`'s `start` command."""
    confdir = tmp_path / "confdir"
    app = build_application(config, confdir=confdir)
    app.master.options.upstream_cert = False

    api = create_control_api(app)
    uvicorn_config = uvicorn.Config(
        api, host=config.control_bind, port=config.control_port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(uvicorn_config)

    async def _run() -> None:
        await asyncio.gather(app.master.run(), server.serve())

    task = asyncio.create_task(_run())
    try:
        proxy_port = await _wait_for_proxy_port(app)
        control_port = await _wait_for_control_port(server)
        yield app, proxy_port, control_port
    finally:
        server.should_exit = True
        await shutdown(app)
        try:
            await asyncio.wait_for(task, timeout=_TEARDOWN_TIMEOUT)
        except asyncio.TimeoutError:  # pragma: no cover -- defensive, shouldn't happen
            task.cancel()


async def _poll_for_pending_request_id(
    control_port: int, *, timeout: float = 5.0
) -> str:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{control_port}", timeout=3.0
    ) as control_client:
        while time.monotonic() < deadline:
            resp = await control_client.get("/mock/pending")
            resp.raise_for_status()
            pending = resp.json()["pending"]
            if pending:
                return pending[0]["request_id"]
            await asyncio.sleep(0.02)
    raise TimeoutError("no pending request appeared via /mock/pending in time")


class TestCrossLoopPendingRespond:
    @pytest.mark.asyncio
    async def test_pending_future_resolved_via_control_api_unblocks_proxy_request(
        self, tmp_path: Path
    ):
        config = make_config(tmp_path)

        async with running_app_with_control_api(config, tmp_path) as (app, proxy_port, control_port):
            async with httpx.AsyncClient(
                proxy=f"http://127.0.0.1:{proxy_port}", verify=False, timeout=6.0
            ) as proxy_client:
                # [1] fire the app request -- it will block on a pending
                # Future until /mock/respond resolves it (or agent_timeout,
                # which the test must beat).
                request_task = asyncio.create_task(
                    proxy_client.post("http://mock-target.test/v1/widgets", json={"id": 1})
                )

                # [2] the SAME loop must still service this control-API
                # call while the request above is blocked -- that's the
                # entire point of this test (D1).
                request_id = await asyncio.wait_for(
                    _poll_for_pending_request_id(control_port), timeout=5.0
                )

                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{control_port}", timeout=3.0
                ) as control_client:
                    respond_resp = await control_client.post(
                        "/mock/respond",
                        json={
                            "request_id": request_id,
                            "status": 200,
                            "body": {"served": "cross-loop"},
                        },
                    )
                    assert respond_resp.status_code == 200
                    assert respond_resp.json()["resolved"] is True

                # [3] the originally-blocked request must now unblock with
                # exactly that body.
                response = await asyncio.wait_for(request_task, timeout=5.0)
                assert response.status_code == 200
                assert response.json() == {"served": "cross-loop"}

                # [4] and a recording must have been written for it.
                recordings = sorted(config.recordings_dir.rglob("*.json"))
                assert len(recordings) == 1
                saved = json.loads(recordings[0].read_text(encoding="utf-8"))
                assert json.loads(saved["response"]["body"]) == {"served": "cross-loop"}
                assert saved["response"]["status_code"] == 200
