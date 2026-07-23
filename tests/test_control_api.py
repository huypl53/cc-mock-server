"""RED-first tests for cc_mock_server.control_api (plan.md phase 7).

Exercised over `httpx.ASGITransport` (not starlette's sync `TestClient`) so
every request runs as a coroutine on the SAME event loop the test itself
runs on (pytest-asyncio `asyncio_mode = auto`) -- this keeps the
`asyncio.Future`s created in some of these tests resolvable inline (no
cross-thread bridging), matching how `create_control_api()` behaves when
embedded in the real single-loop deployment (see `test_cross_loop.py` for
the end-to-end proof over the real bootstrap).

`Application` is built here from real (not-mitmproxy) components --
`DomainFilter`, `EndpointSelector`, `Recorder`, `AgentHandler`,
`ModeRouter` -- exactly as `app.build_components()` does, but `addon` and
`master` are left `None` since `control_api.py` never touches those fields
(D6: it only reads/mutates `config`/`domain_filter`/`selector`/`recorder`/
`agent_handler`).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote

import httpx
import pytest

from cc_mock_server.agent_handler import AgentHandler
from cc_mock_server.app import Application
from cc_mock_server.config import Config
from cc_mock_server.control_api import create_control_api
from cc_mock_server.enums import AgentMode, FilterMode, Mode
from cc_mock_server.filter import DomainFilter
from cc_mock_server.matcher import fuzzy_key, match
from cc_mock_server.models import PendingRequest, Request, Response
from cc_mock_server.recorder import Recorder
from cc_mock_server.router import ModeRouter
from cc_mock_server.selector import EndpointSelector


@pytest.fixture
async def application(tmp_path: Path) -> AsyncIterator[Application]:
    config = Config(
        recordings_dir=tmp_path / "recordings",
        filter_mode=FilterMode.WHITELIST,
        filter_domains=["allowed.example.com"],
        agent_mode=AgentMode.PENDING,
        agent_timeout=1.0,
    )
    domain_filter = DomainFilter(mode=config.filter_mode, domains=config.filter_domains)
    selector = EndpointSelector()
    recorder = Recorder(config.recordings_dir)
    recorder.load_all()
    agent_handler = AgentHandler(config)
    router = ModeRouter(
        config, domain_filter, selector, agent_handler, recorder, match_fn=match, fuzzy_key_fn=fuzzy_key
    )
    app = Application(
        config=config,
        domain_filter=domain_filter,
        selector=selector,
        recorder=recorder,
        agent_handler=agent_handler,
        router=router,
        addon=None,  # type: ignore[arg-type]
        master=None,  # type: ignore[arg-type]
    )
    try:
        yield app
    finally:
        await agent_handler.aclose()


@pytest.fixture
async def client(application: Application) -> AsyncIterator[httpx.AsyncClient]:
    api = create_control_api(application)
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def make_request(**overrides) -> Request:
    defaults = dict(
        method="GET",
        url="http://api.example.com/v1/x",
        host="api.example.com",
        path="/v1/x",
    )
    defaults.update(overrides)
    return Request(**defaults)


def make_response(**overrides) -> Response:
    defaults = dict(
        status_code=200,
        headers={"content-type": "application/json"},
        body="{}",
        is_json=True,
        content_type="application/json",
    )
    defaults.update(overrides)
    return Response(**defaults)


# --------------------------------------------------------------------------
# status / mode
# --------------------------------------------------------------------------


class TestStatus:
    @pytest.mark.asyncio
    async def test_status_reports_current_state(self, client: httpx.AsyncClient):
        resp = await client.get("/mock/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "live"
        assert body["agent_mode"] == "pending"
        assert body["filter_mode"] == "whitelist"
        assert body["filter_domains"] == ["allowed.example.com"]
        assert body["pending_count"] == 0
        assert body["recordings_count"] == 0


class TestModeSwitch:
    @pytest.mark.asyncio
    async def test_mode_switch_mutates_shared_config(
        self, application: Application, client: httpx.AsyncClient
    ):
        assert application.config.mode == Mode.LIVE

        resp = await client.post("/mock/mode", json={"mode": "replay"})

        assert resp.status_code == 200
        assert resp.json() == {"mode": "replay"}
        assert application.config.mode == Mode.REPLAY

    @pytest.mark.asyncio
    async def test_mode_switch_rejects_invalid_mode(self, client: httpx.AsyncClient):
        resp = await client.post("/mock/mode", json={"mode": "not-a-mode"})
        assert resp.status_code == 422


# --------------------------------------------------------------------------
# filter (D7)
# --------------------------------------------------------------------------


class TestFilter:
    @pytest.mark.asyncio
    async def test_get_filter_lists_current_domains(self, client: httpx.AsyncClient):
        resp = await client.get("/mock/filter")
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "whitelist"
        assert body["domains"] == ["allowed.example.com"]

    @pytest.mark.asyncio
    async def test_add_and_remove_domain_reflected_in_domain_filter(
        self, application: Application, client: httpx.AsyncClient
    ):
        add_resp = await client.post("/mock/filter", json={"action": "add", "domain": "new.example.com"})
        assert add_resp.status_code == 200
        assert "new.example.com" in application.domain_filter.list_domains()
        assert "new.example.com" in add_resp.json()["domains"]

        remove_resp = await client.post(
            "/mock/filter", json={"action": "remove", "domain": "new.example.com"}
        )
        assert remove_resp.status_code == 200
        assert "new.example.com" not in application.domain_filter.list_domains()


# --------------------------------------------------------------------------
# select / deselect
# --------------------------------------------------------------------------


class TestSelect:
    @pytest.mark.asyncio
    async def test_select_then_deselect_flip_is_selected(
        self, application: Application, client: httpx.AsyncClient
    ):
        pattern = "GET api.example.com/v1/x"
        request = make_request()

        select_resp = await client.post("/mock/select", json={"pattern": pattern})
        assert select_resp.status_code == 200
        assert application.selector.is_selected(request) is True

        deselect_resp = await client.delete(f"/mock/select/{quote(pattern, safe='/')}")
        assert deselect_resp.status_code == 200
        assert application.selector.is_selected(request) is False

    @pytest.mark.asyncio
    async def test_get_select_reports_overrides(self, client: httpx.AsyncClient):
        await client.post("/mock/select", json={"pattern": "domain.example.com"})

        resp = await client.get("/mock/select")

        assert resp.status_code == 200
        body = resp.json()
        assert "auto_select_filtered" in body
        assert body["overrides"]["domain"]["domain.example.com"] is True


# --------------------------------------------------------------------------
# pending / respond (D1/D2)
# --------------------------------------------------------------------------


class TestPendingAndRespond:
    @pytest.mark.asyncio
    async def test_pending_lists_in_flight_and_respond_resolves_future(
        self, application: Application, client: httpx.AsyncClient
    ):
        request = make_request()
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Response]" = loop.create_future()
        application.agent_handler.pending["req-1"] = PendingRequest(
            request_id="req-1",
            request=request,
            future=future,
            created_at=datetime.now(timezone.utc),
        )

        pending_resp = await client.get("/mock/pending")
        assert pending_resp.status_code == 200
        pending_body = pending_resp.json()["pending"]
        assert len(pending_body) == 1
        assert pending_body[0]["request_id"] == "req-1"
        assert pending_body[0]["request"]["host"] == "api.example.com"

        respond_resp = await client.post(
            "/mock/respond",
            json={"request_id": "req-1", "status": 201, "body": {"ok": 1}},
        )
        assert respond_resp.status_code == 200
        assert respond_resp.json() == {"request_id": "req-1", "resolved": True}

        resolved_response = await asyncio.wait_for(future, timeout=1.0)
        assert resolved_response.status_code == 201
        assert json.loads(resolved_response.body) == {"ok": 1}

    @pytest.mark.asyncio
    async def test_pending_masks_sensitive_headers(
        self, application: Application, client: httpx.AsyncClient
    ):
        # D3: the agent polls GET /mock/pending, so it is a secret-egress
        # path -- Authorization/cookie must never reach the agent in clear.
        request = make_request(
            headers={
                "Authorization": "Bearer secret123",
                "Cookie": "session=abc",
                "Accept": "*/*",
            }
        )
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Response]" = loop.create_future()
        application.agent_handler.pending["req-mask"] = PendingRequest(
            request_id="req-mask",
            request=request,
            future=future,
            created_at=datetime.now(timezone.utc),
        )

        resp = await client.get("/mock/pending")
        assert resp.status_code == 200
        headers = resp.json()["pending"][0]["request"]["headers"]
        assert headers["Authorization"] == "***"
        assert headers["Cookie"] == "***"
        assert headers["Accept"] == "*/*"  # non-sensitive untouched
        # secret value absent anywhere in the serialized payload
        assert "secret123" not in resp.text
        assert "session=abc" not in resp.text

        future.cancel()

    @pytest.mark.asyncio
    async def test_respond_unknown_request_id_returns_404(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/mock/respond", json={"request_id": "does-not-exist", "status": 200, "body": {}}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_respond_without_id_auto_targets_sole_pending(
        self, application: Application, client: httpx.AsyncClient
    ):
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Response]" = loop.create_future()
        application.agent_handler.pending["only-one"] = PendingRequest(
            request_id="only-one",
            request=make_request(),
            future=future,
            created_at=datetime.now(timezone.utc),
        )

        # No request_id in the body -- auto-target the single pending request.
        resp = await client.post("/mock/respond", json={"status": 200, "body": {"ok": 1}})
        assert resp.status_code == 200
        assert resp.json() == {"request_id": "only-one", "resolved": True}

        resolved = await asyncio.wait_for(future, timeout=1.0)
        assert json.loads(resolved.body) == {"ok": 1}

    @pytest.mark.asyncio
    async def test_respond_without_id_no_pending_returns_404(self, client: httpx.AsyncClient):
        resp = await client.post("/mock/respond", json={"status": 200, "body": {}})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_respond_without_id_multiple_pending_returns_409(
        self, application: Application, client: httpx.AsyncClient
    ):
        loop = asyncio.get_running_loop()
        for rid in ("req-a", "req-b"):
            application.agent_handler.pending[rid] = PendingRequest(
                request_id=rid,
                request=make_request(),
                future=loop.create_future(),
                created_at=datetime.now(timezone.utc),
            )

        resp = await client.post("/mock/respond", json={"status": 200, "body": {}})
        assert resp.status_code == 409
        assert "req-a" in resp.text and "req-b" in resp.text

        for pending in application.agent_handler.pending.values():
            pending.future.cancel()


# --------------------------------------------------------------------------
# recordings
# --------------------------------------------------------------------------


class TestRecordings:
    @pytest.mark.asyncio
    async def test_list_and_delete_recording(
        self, application: Application, client: httpx.AsyncClient
    ):
        recording = await application.recorder.save(make_request(), make_response())

        list_resp = await client.get("/mock/recordings")
        assert list_resp.status_code == 200
        ids = [r["id"] for r in list_resp.json()["recordings"]]
        assert recording.id in ids

        delete_resp = await client.delete(f"/mock/recordings/{recording.id}")
        assert delete_resp.status_code == 200
        assert delete_resp.json() == {"deleted": recording.id}

        list_resp_2 = await client.get("/mock/recordings")
        assert recording.id not in [r["id"] for r in list_resp_2.json()["recordings"]]

    @pytest.mark.asyncio
    async def test_delete_unknown_recording_returns_404(self, client: httpx.AsyncClient):
        resp = await client.delete("/mock/recordings/does-not-exist")
        assert resp.status_code == 404


# --------------------------------------------------------------------------
# config (D3): non-loopback agent_url MUST be rejected with 400
# --------------------------------------------------------------------------


class TestConfigUpdate:
    @pytest.mark.asyncio
    async def test_rejects_non_loopback_agent_url(
        self, application: Application, client: httpx.AsyncClient
    ):
        resp = await client.post("/mock/config", json={"agent_url": "http://evil.example.com/callback"})

        assert resp.status_code == 400
        assert application.config.agent_url is None

    @pytest.mark.asyncio
    async def test_accepts_loopback_agent_url_and_other_fields(
        self, application: Application, client: httpx.AsyncClient
    ):
        resp = await client.post(
            "/mock/config",
            json={"agent_url": "http://127.0.0.1:9999/cb", "agent_timeout": 2.5, "min_confidence": 0.8},
        )

        assert resp.status_code == 200
        assert application.config.agent_url == "http://127.0.0.1:9999/cb"
        assert application.config.agent_timeout == 2.5
        assert application.config.min_confidence == 0.8

    @pytest.mark.asyncio
    async def test_rejects_invalid_min_confidence(self, client: httpx.AsyncClient):
        resp = await client.post("/mock/config", json={"min_confidence": 1.5})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_partial_update_leaves_unset_fields_untouched(
        self, application: Application, client: httpx.AsyncClient
    ):
        original_timeout = application.config.agent_timeout
        resp = await client.post("/mock/config", json={"min_confidence": 0.9})
        assert resp.status_code == 200
        assert application.config.agent_timeout == original_timeout
        assert application.config.min_confidence == 0.9
