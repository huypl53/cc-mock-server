"""RED-first tests for cc_mock_server.agent_handler (plan.md phase 5).

Covers every phase-05 success criterion: sync XOR pending transport (D2),
double-respond guard + cross-request isolation (D1/D2), every
timeout_fallback strategy with an injected fake clock (no wall-clock
sleeps), client-disconnect cancellation without recording (D5),
`max_pending` admission control, secret masking toward the agent (D3),
non-loopback `agent_url` rejection (D3), binary-body base64 forwarding
(D8), and the built-in handler contract.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from pytest_httpserver import HTTPServer

from cc_mock_server.agent_handler import AgentHandler, ClientDisconnected
from cc_mock_server.config import Config
from cc_mock_server.enums import AgentMode, TimeoutFallback
from cc_mock_server.models import Request


def make_request(**overrides: Any) -> Request:
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


def make_config(**overrides: Any) -> Config:
    defaults: dict[str, Any] = dict(agent_timeout=5.0)
    defaults.update(overrides)
    return Config(**defaults)


async def instant_timeout(_seconds: float) -> None:
    """Fake clock (phase-5 success criterion): resolves on the very next
    loop iteration regardless of the requested duration, so timeout tests
    are deterministic and never rely on real wall-clock delays."""
    await asyncio.sleep(0)


async def never_fires(_seconds: float) -> None:
    """Fake clock that never completes on its own — used when a test wants
    to prove the *future* (not the timeout) won it, without racing real
    time."""
    await asyncio.Event().wait()


@pytest.fixture
async def handler_factory():
    created: list[AgentHandler] = []

    def _make(config: Config, **kwargs: Any) -> AgentHandler:
        handler = AgentHandler(config, **kwargs)
        created.append(handler)
        return handler

    yield _make

    for handler in created:
        await handler.aclose()


# --------------------------------------------------------------------------
# sync transport (D2)
# --------------------------------------------------------------------------


class TestSyncTransport:
    @pytest.mark.asyncio
    async def test_sync_wraps_agent_json_into_200_response(
        self, handler_factory, httpserver: HTTPServer
    ):
        httpserver.expect_request("/agent", method="POST").respond_with_json(
            {"status_code": 200, "body": {"ok": True}}
        )
        config = make_config(
            agent_mode=AgentMode.SYNC, agent_url=httpserver.url_for("/agent")
        )
        handler = handler_factory(config)

        result = await handler.handle(make_request())

        assert result.action == "respond"
        assert result.response.status_code == 200
        assert json.loads(result.response.body) == {"ok": True}
        assert result.response.is_json is True
        # sync mode never creates a pending future
        assert handler.pending == {}

    @pytest.mark.asyncio
    async def test_sync_does_not_create_pending_future(
        self, handler_factory, httpserver: HTTPServer
    ):
        httpserver.expect_request("/agent", method="POST").respond_with_json(
            {"status_code": 201, "body": {"created": True}}
        )
        config = make_config(
            agent_mode=AgentMode.SYNC, agent_url=httpserver.url_for("/agent")
        )
        handler = handler_factory(config)

        result = await handler.handle(make_request())

        assert result.response.status_code == 201
        assert handler.pending == {}


# --------------------------------------------------------------------------
# pending transport + respond() resolution safety (D1/D2)
# --------------------------------------------------------------------------


class TestPendingRespond:
    @pytest.mark.asyncio
    async def test_respond_resolves_only_the_matching_request_id(self, handler_factory):
        config = make_config(agent_mode=AgentMode.PENDING, agent_timeout=5.0)
        handler = handler_factory(config, sleep=never_fires)

        task_a = asyncio.create_task(handler.handle(make_request(path="/a")))
        task_b = asyncio.create_task(handler.handle(make_request(path="/b")))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(handler.pending) == 2

        request_ids = list(handler.pending.keys())
        target_id = request_ids[0]
        other_id = request_ids[1]

        ok = handler.respond(target_id, 200, {"resolved": True})
        assert ok is True

        # whichever task owned target_id must finish; the other stays pending
        done, pending_tasks = await asyncio.wait(
            {task_a, task_b}, timeout=0.2, return_when=asyncio.FIRST_COMPLETED
        )
        assert len(done) == 1
        finished_task = done.pop()
        result = finished_task.result()
        assert result.action == "respond"
        assert json.loads(result.response.body) == {"resolved": True}

        # the other request must still be pending (not resolved, not popped)
        assert other_id in handler.pending
        still_pending_task = task_b if finished_task is task_a else task_a
        assert not still_pending_task.done()

        # cleanup
        still_pending_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await still_pending_task

    @pytest.mark.asyncio
    async def test_double_respond_same_id_does_not_raise(self, handler_factory):
        config = make_config(agent_mode=AgentMode.PENDING, agent_timeout=5.0)
        handler = handler_factory(config, sleep=never_fires)

        task = asyncio.create_task(handler.handle(make_request()))
        await asyncio.sleep(0)
        request_id = next(iter(handler.pending))

        first = handler.respond(request_id, 200, {"first": True})
        # second respond for the same id: must not raise InvalidStateError
        second = handler.respond(request_id, 200, {"second": True})
        assert first is True

        result = await asyncio.wait_for(task, timeout=1.0)
        # first respond wins; body must be from the first call, not the second
        assert json.loads(result.response.body) == {"first": True}
        # respond() itself returned True even for the (guarded) no-op second call
        # because the pending entry hadn't been popped yet.
        assert second is True

    @pytest.mark.asyncio
    async def test_respond_unknown_request_id_returns_false(self, handler_factory):
        config = make_config(agent_mode=AgentMode.PENDING)
        handler = handler_factory(config)
        assert handler.respond("does-not-exist", 200, {}) is False

    @pytest.mark.asyncio
    async def test_respond_with_loop_uses_call_soon_threadsafe(self, handler_factory):
        config = make_config(agent_mode=AgentMode.PENDING, agent_timeout=5.0)
        handler = handler_factory(config, sleep=never_fires)

        task = asyncio.create_task(handler.handle(make_request()))
        await asyncio.sleep(0)
        request_id = next(iter(handler.pending))

        loop = asyncio.get_running_loop()
        ok = handler.respond(request_id, 200, {"via": "loop"}, loop=loop)
        assert ok is True

        result = await asyncio.wait_for(task, timeout=1.0)
        assert json.loads(result.response.body) == {"via": "loop"}

    @pytest.mark.asyncio
    async def test_pending_dict_empty_after_successful_respond(self, handler_factory):
        config = make_config(agent_mode=AgentMode.PENDING, agent_timeout=5.0)
        handler = handler_factory(config, sleep=never_fires)

        task = asyncio.create_task(handler.handle(make_request()))
        await asyncio.sleep(0)
        request_id = next(iter(handler.pending))
        handler.respond(request_id, 200, {})
        await asyncio.wait_for(task, timeout=1.0)

        assert handler.pending == {}


# --------------------------------------------------------------------------
# timeout fallback (D5), fake clock only — no wall-clock sleeps
# --------------------------------------------------------------------------


class TestTimeoutFallback:
    @pytest.mark.asyncio
    async def test_timeout_return_error_yields_504(self, handler_factory):
        config = make_config(
            agent_mode=AgentMode.PENDING, timeout_fallback=TimeoutFallback.RETURN_ERROR
        )
        handler = handler_factory(config, sleep=instant_timeout)

        result = await handler.handle(make_request())

        assert result.action == "respond"
        assert result.response.status_code == 504
        assert handler.pending == {}

    @pytest.mark.asyncio
    async def test_timeout_pass_through_yields_pass_through_action(self, handler_factory):
        config = make_config(
            agent_mode=AgentMode.PENDING, timeout_fallback=TimeoutFallback.PASS_THROUGH
        )
        handler = handler_factory(config, sleep=instant_timeout)

        result = await handler.handle(make_request())

        assert result.action == "pass_through"
        assert result.response is None
        assert handler.pending == {}

    @pytest.mark.asyncio
    async def test_timeout_built_in_yields_built_in_response(self, handler_factory):
        config = make_config(
            agent_mode=AgentMode.PENDING, timeout_fallback=TimeoutFallback.BUILT_IN
        )
        handler = handler_factory(config, sleep=instant_timeout)

        result = await handler.handle(make_request(body='{"echo": 1}', is_json=True))

        assert result.action == "respond"
        assert result.response.status_code == 200
        assert result.response.headers["content-type"] == "application/json"
        assert json.loads(result.response.body) == {"echo": 1}
        assert handler.pending == {}


# --------------------------------------------------------------------------
# client disconnect (D5)
# --------------------------------------------------------------------------


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_cancels_pending_and_raises_without_recording(
        self, handler_factory
    ):
        config = make_config(agent_mode=AgentMode.PENDING, agent_timeout=5.0)
        handler = handler_factory(config, sleep=never_fires)
        on_disconnect = asyncio.Event()

        task = asyncio.create_task(handler.handle(make_request(), on_disconnect=on_disconnect))
        await asyncio.sleep(0)
        assert len(handler.pending) == 1

        on_disconnect.set()

        with pytest.raises(ClientDisconnected):
            await asyncio.wait_for(task, timeout=1.0)

        # finally must always pop -> no leak, and nothing left to record
        assert handler.pending == {}

    @pytest.mark.asyncio
    async def test_disconnect_after_respond_prefers_the_response(self, handler_factory):
        """If respond() and disconnect race, an already-resolved future
        must win (respond() happened first)."""
        config = make_config(agent_mode=AgentMode.PENDING, agent_timeout=5.0)
        handler = handler_factory(config, sleep=never_fires)
        on_disconnect = asyncio.Event()

        task = asyncio.create_task(handler.handle(make_request(), on_disconnect=on_disconnect))
        await asyncio.sleep(0)
        request_id = next(iter(handler.pending))
        handler.respond(request_id, 200, {"ok": True})

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result.action == "respond"
        assert json.loads(result.response.body) == {"ok": True}


# --------------------------------------------------------------------------
# max_pending admission control (D5)
# --------------------------------------------------------------------------


class TestMaxPending:
    @pytest.mark.asyncio
    async def test_max_pending_cap_returns_503_without_new_pending_entry(self, handler_factory):
        config = make_config(agent_mode=AgentMode.PENDING, max_pending=1, agent_timeout=5.0)
        handler = handler_factory(config, sleep=never_fires)

        first_task = asyncio.create_task(handler.handle(make_request()))
        await asyncio.sleep(0)
        assert len(handler.pending) == 1

        result = await handler.handle(make_request(path="/second"))

        assert result.action == "respond"
        assert result.response.status_code == 503
        # capacity must not have been consumed by the rejected request
        assert len(handler.pending) == 1

        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task


# --------------------------------------------------------------------------
# secret masking toward the agent (D3)
# --------------------------------------------------------------------------


class TestSecretMasking:
    @pytest.mark.asyncio
    async def test_authorization_absent_from_sync_payload(
        self, handler_factory, httpserver: HTTPServer
    ):
        httpserver.expect_request("/agent", method="POST").respond_with_json(
            {"status_code": 200, "body": {"ok": True}}
        )
        config = make_config(agent_mode=AgentMode.SYNC, agent_url=httpserver.url_for("/agent"))
        handler = handler_factory(config)

        req = make_request(
            headers={
                "Authorization": "Bearer super-secret",
                "X-Api-Key": "sk-secret",
                "Cookie": "session=secret",
                "Content-Type": "application/json",
            }
        )
        await handler.handle(req)

        sent_headers = httpserver.log[-1][0].get_json()["headers"]
        assert "super-secret" not in json.dumps(sent_headers)
        assert "sk-secret" not in json.dumps(sent_headers)
        assert "session=secret" not in json.dumps(sent_headers)
        assert sent_headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_authorization_absent_from_pending_notify_payload(
        self, handler_factory, httpserver: HTTPServer
    ):
        httpserver.expect_request("/agent", method="POST").respond_with_data(
            "", status=202
        )
        config = make_config(
            agent_mode=AgentMode.PENDING,
            agent_url=httpserver.url_for("/agent"),
            agent_timeout=5.0,
        )
        handler = handler_factory(config, sleep=never_fires)

        req = make_request(headers={"Authorization": "Bearer super-secret"})
        task = asyncio.create_task(handler.handle(req))
        await asyncio.sleep(0)
        # the notify POST is real (fire-and-forget) I/O over loopback: poll
        # with real (short) sleeps rather than fake clock ticks, since the
        # underlying socket round-trip needs actual OS scheduling.
        for _ in range(50):
            if httpserver.log:
                break
            await asyncio.sleep(0.02)

        assert len(httpserver.log) == 1
        sent_body = httpserver.log[-1][0].get_json()
        assert "super-secret" not in json.dumps(sent_body["headers"])

        request_id = next(iter(handler.pending))
        handler.respond(request_id, 200, {})
        await asyncio.wait_for(task, timeout=1.0)


# --------------------------------------------------------------------------
# non-loopback agent_url rejection (D3)
# --------------------------------------------------------------------------


class TestLoopbackGuard:
    def test_non_loopback_agent_url_is_rejected(self):
        config = make_config(
            agent_mode=AgentMode.SYNC, agent_url="http://evil.example.com/agent"
        )
        with pytest.raises(ValueError):
            AgentHandler(config)

    def test_loopback_agent_url_is_accepted(self):
        config = make_config(agent_mode=AgentMode.SYNC, agent_url="http://127.0.0.1:9999/agent")
        handler = AgentHandler(config)
        assert handler is not None


# --------------------------------------------------------------------------
# binary body -> base64 forwarded in payload (D8)
# --------------------------------------------------------------------------


class TestBinaryBodyPayload:
    @pytest.mark.asyncio
    async def test_binary_body_forwarded_as_base64_in_payload(
        self, handler_factory, httpserver: HTTPServer
    ):
        raw = bytes(range(256))
        req = Request.from_raw(
            method="POST",
            url="http://example.com/upload",
            host="example.com",
            path="/upload",
            raw_body=raw,
            content_type="application/octet-stream",
        )
        assert req.is_json is False

        httpserver.expect_request("/agent", method="POST").respond_with_json(
            {"status_code": 200, "body": {"ok": True}}
        )
        config = make_config(agent_mode=AgentMode.SYNC, agent_url=httpserver.url_for("/agent"))
        handler = handler_factory(config)

        await handler.handle(req)

        sent_body = httpserver.log[-1][0].get_json()
        assert sent_body["is_json"] is False
        assert sent_body["content_type"] == "application/octet-stream"
        assert base64.b64decode(sent_body["body"]) == raw


# --------------------------------------------------------------------------
# built-in handler contract
# --------------------------------------------------------------------------


class TestBuiltInHandler:
    def test_built_in_echoes_json_body(self):
        config = make_config(agent_mode=AgentMode.PENDING)
        handler = AgentHandler(config)
        req = make_request(body='{"a": 1}', is_json=True, content_type="application/json")

        response = handler.built_in_response(req)

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert json.loads(response.body) == {"a": 1}

    def test_built_in_defaults_to_empty_object_for_non_json_body(self):
        config = make_config(agent_mode=AgentMode.PENDING)
        handler = AgentHandler(config)
        req = make_request(body="not json", is_json=False, content_type="text/plain")

        response = handler.built_in_response(req)

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert json.loads(response.body) == {}

    def test_built_in_defaults_to_empty_object_for_empty_body(self):
        config = make_config(agent_mode=AgentMode.PENDING)
        handler = AgentHandler(config)
        req = make_request(body="", is_json=False, content_type=None)

        response = handler.built_in_response(req)
        assert json.loads(response.body) == {}
