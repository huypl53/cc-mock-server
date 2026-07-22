"""RED-first tests for cc_mock_server.router (plan.md phase 6).

`ModeRouter` is exercised entirely against fakes for `DomainFilter`,
`EndpointSelector`, `AgentHandler`, `Recorder`, and the matcher functions --
no mitmproxy involved here (that's `tests/test_proxy_integration.py`).
Covers every branch from phase-06's Implementation Steps #1: CONNECT/domain
filter pass-through, replay hit, replay miss -> each `replay_miss_strategy`,
live+selected -> handler+record, timeout fallback (both `pass_through` and
`respond` shaped fallbacks), client disconnect -> no record, mode/selector
state-snapshot-at-entry (D6), and concurrent recorder mutation during
`match()` (D6, using the real `Recorder`/`matcher` for that one test).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

import pytest

from cc_mock_server.agent_handler import ClientDisconnected
from cc_mock_server.config import Config
from cc_mock_server.enums import AgentMode, FilterMode, Mode, ReplayMissStrategy
from cc_mock_server.matcher import MatchResult
from cc_mock_server.matcher import fuzzy_key as real_fuzzy_key
from cc_mock_server.matcher import match as real_match
from cc_mock_server.models import HandlerResult, Recording, RecordingMetadata, Request, Response
from cc_mock_server.recorder import Recorder
from cc_mock_server.router import ModeRouter

# --------------------------------------------------------------------------
# test doubles
# --------------------------------------------------------------------------


def make_request(**overrides: Any) -> Request:
    defaults = dict(
        method="GET",
        url="http://api.example.com/v1/widgets",
        host="api.example.com",
        path="/v1/widgets",
        query={},
        headers={"accept": "application/json"},
        body="",
        is_json=False,
        content_type=None,
    )
    defaults.update(overrides)
    return Request(**defaults)


def make_config(**overrides: Any) -> Config:
    defaults: dict[str, Any] = {}
    defaults.update(overrides)
    return Config(**defaults)


def make_response(**overrides: Any) -> Response:
    defaults = dict(
        status_code=200,
        headers={"content-type": "application/json"},
        body='{"ok": true}',
        is_json=True,
        content_type="application/json",
    )
    defaults.update(overrides)
    return Response(**defaults)


def make_recording(response: Optional[Response] = None, **request_overrides: Any) -> Recording:
    request = make_request(**request_overrides)
    return Recording(
        id="rec-fixed",
        request=request,
        response=response or make_response(),
        metadata=RecordingMetadata(recorded_at=datetime.now(timezone.utc), source="live"),
    )


class FakeDomainFilter:
    """Stands in for `DomainFilter`: records every `should_intercept` call."""

    def __init__(self, intercept: bool = True) -> None:
        self.intercept = intercept
        self.calls: list[str] = []

    def should_intercept(self, host: str) -> bool:
        self.calls.append(host)
        return self.intercept


class FakeSelector:
    """Stands in for `EndpointSelector`: records every `is_selected` call."""

    def __init__(self, selected: bool = True) -> None:
        self.selected = selected
        self.calls: list[Request] = []

    def is_selected(self, request: Request) -> bool:
        self.calls.append(request)
        return self.selected


HandleFn = Callable[[Request, Optional[asyncio.Event]], Awaitable[HandlerResult]]


class FakeAgentHandler:
    """Stands in for `AgentHandler`: delegates `.handle()` to an injected
    async function so each test can script success/timeout/disconnect."""

    def __init__(self, handle_fn: HandleFn) -> None:
        self._handle_fn = handle_fn
        self.calls: list[tuple[Request, Optional[asyncio.Event]]] = []

    async def handle(
        self, request: Request, on_disconnect: Optional[asyncio.Event] = None
    ) -> HandlerResult:
        self.calls.append((request, on_disconnect))
        return await self._handle_fn(request, on_disconnect)


def never_called_handle_fn(_request: Request, _on_disconnect: Optional[asyncio.Event]) -> Any:
    raise AssertionError("agent_handler.handle must not be called on this branch")


@dataclass
class FakeRecorder:
    """Stands in for `Recorder`: `snapshot()` returns a fixed tuple, `save()`
    records every call and returns a freshly built `Recording` (mirroring
    `Recorder.save`'s always-`fuzzy_key=None` contract, H4)."""

    recordings: tuple[Recording, ...] = ()
    saved: list[tuple[Request, Response, str]] = field(default_factory=list)

    def snapshot(self) -> tuple[Recording, ...]:
        return self.recordings

    async def save(self, request: Request, response: Response, *, source: str = "live") -> Recording:
        self.saved.append((request, response, source))
        return Recording(
            id=f"rec-{len(self.saved)}",
            request=request,
            response=response,
            metadata=RecordingMetadata(
                recorded_at=datetime.now(timezone.utc), source=source, fuzzy_key=None
            ),
        )


def make_match_fn(result: Optional[MatchResult]):
    calls: list[tuple[Request, tuple, float]] = []

    def _match_fn(request: Request, candidates: Iterable[Recording], min_confidence: float):
        calls.append((request, tuple(candidates), min_confidence))
        return result

    _match_fn.calls = calls  # type: ignore[attr-defined]
    return _match_fn


def build_router(
    *,
    config: Optional[Config] = None,
    domain_filter: Optional[FakeDomainFilter] = None,
    selector: Optional[FakeSelector] = None,
    agent_handler: Optional[FakeAgentHandler] = None,
    recorder: Optional[FakeRecorder] = None,
    match_fn=None,
    fuzzy_key_fn=None,
) -> ModeRouter:
    return ModeRouter(
        config or make_config(),
        domain_filter if domain_filter is not None else FakeDomainFilter(intercept=True),
        selector if selector is not None else FakeSelector(selected=True),
        agent_handler
        if agent_handler is not None
        else FakeAgentHandler(never_called_handle_fn),
        recorder if recorder is not None else FakeRecorder(),
        match_fn=match_fn if match_fn is not None else make_match_fn(None),
        fuzzy_key_fn=fuzzy_key_fn if fuzzy_key_fn is not None else (lambda request: "fake-key"),
    )


# --------------------------------------------------------------------------
# [0] domain filter pass-through
# --------------------------------------------------------------------------


class TestDomainFilterPassThrough:
    @pytest.mark.asyncio
    async def test_filtered_out_domain_is_pass_through_and_touches_nothing_else(self):
        domain_filter = FakeDomainFilter(intercept=False)
        selector = FakeSelector(selected=True)
        agent_handler = FakeAgentHandler(never_called_handle_fn)
        recorder = FakeRecorder()
        match_fn = make_match_fn(None)

        router = build_router(
            domain_filter=domain_filter,
            selector=selector,
            agent_handler=agent_handler,
            recorder=recorder,
            match_fn=match_fn,
        )

        result = await router.route(make_request(host="out-of-scope.example.com"))

        assert result.action == "pass_through"
        assert result.response is None
        assert domain_filter.calls == ["out-of-scope.example.com"]
        # nothing downstream of the filter should ever be touched
        assert selector.calls == []
        assert agent_handler.calls == []
        assert recorder.saved == []
        assert match_fn.calls == []


# --------------------------------------------------------------------------
# [2] replay hit
# --------------------------------------------------------------------------


class TestReplayHit:
    @pytest.mark.asyncio
    async def test_replay_hit_returns_recording_response_without_agent_or_recording(self):
        recording = make_recording(response=make_response(body='{"cached": true}'))
        match_fn = make_match_fn(MatchResult(recording=recording, confidence=0.9))
        agent_handler = FakeAgentHandler(never_called_handle_fn)
        recorder = FakeRecorder(recordings=(recording,))

        router = build_router(
            config=make_config(mode=Mode.REPLAY),
            agent_handler=agent_handler,
            recorder=recorder,
            match_fn=match_fn,
        )

        result = await router.route(make_request())

        assert result.action == "respond"
        assert result.response is recording.response
        assert agent_handler.calls == []
        assert recorder.saved == []
        # matcher was given the recorder's snapshot + configured min_confidence
        assert match_fn.calls[0][1] == (recording,)
        assert match_fn.calls[0][2] == make_config().min_confidence


# --------------------------------------------------------------------------
# [2] replay miss -> each replay_miss_strategy
# --------------------------------------------------------------------------


class TestReplayMissStrategies:
    @pytest.mark.asyncio
    async def test_miss_pass_through(self):
        router = build_router(
            config=make_config(mode=Mode.REPLAY, replay_miss_strategy=ReplayMissStrategy.PASS_THROUGH),
            match_fn=make_match_fn(None),
        )

        result = await router.route(make_request())

        assert result.action == "pass_through"
        assert result.response is None

    @pytest.mark.asyncio
    async def test_miss_return_error(self):
        router = build_router(
            config=make_config(
                mode=Mode.REPLAY, replay_miss_strategy=ReplayMissStrategy.RETURN_ERROR
            ),
            match_fn=make_match_fn(None),
        )

        result = await router.route(make_request())

        assert result.action == "respond"
        assert result.response.status_code == 502
        assert json.loads(result.response.body)["error"]

    @pytest.mark.asyncio
    async def test_miss_live_calls_agent_and_records(self):
        agent_response = make_response(body='{"from": "agent"}')

        async def handle_fn(request, on_disconnect):
            return HandlerResult(action="respond", response=agent_response)

        agent_handler = FakeAgentHandler(handle_fn)
        recorder = FakeRecorder()

        router = build_router(
            config=make_config(mode=Mode.REPLAY, replay_miss_strategy=ReplayMissStrategy.LIVE),
            agent_handler=agent_handler,
            recorder=recorder,
            match_fn=make_match_fn(None),
        )

        result = await router.route(make_request())

        assert result.action == "respond"
        assert result.response is agent_response
        assert len(agent_handler.calls) == 1
        assert len(recorder.saved) == 1
        assert recorder.saved[0][1] is agent_response
        assert recorder.saved[0][2] == "live"


# --------------------------------------------------------------------------
# [3] live + selected -> handler + record; unselected -> replay path
# --------------------------------------------------------------------------


class TestLiveSelected:
    @pytest.mark.asyncio
    async def test_live_selected_calls_agent_and_records_with_fuzzy_key_backfilled(self):
        agent_response = make_response(body='{"live": true}')

        async def handle_fn(request, on_disconnect):
            return HandlerResult(action="respond", response=agent_response)

        agent_handler = FakeAgentHandler(handle_fn)
        recorder = FakeRecorder()
        request = make_request()

        router = build_router(
            config=make_config(mode=Mode.LIVE),
            selector=FakeSelector(selected=True),
            agent_handler=agent_handler,
            recorder=recorder,
            match_fn=make_match_fn(None),  # must not even be consulted (selected wins)
            fuzzy_key_fn=lambda req: "GET::api.example.com::/v1/widgets",
        )

        result = await router.route(request)

        assert result.action == "respond"
        assert result.response is agent_response
        assert len(recorder.saved) == 1
        saved_request, saved_response, source = recorder.saved[0]
        assert saved_request is request
        assert saved_response is agent_response
        assert source == "live"

    @pytest.mark.asyncio
    async def test_live_unselected_falls_back_to_replay_match(self):
        recording = make_recording(response=make_response(body='{"cached": true}'))
        match_fn = make_match_fn(MatchResult(recording=recording, confidence=0.95))

        router = build_router(
            config=make_config(mode=Mode.LIVE),
            selector=FakeSelector(selected=False),
            agent_handler=FakeAgentHandler(never_called_handle_fn),
            recorder=FakeRecorder(recordings=(recording,)),
            match_fn=match_fn,
        )

        result = await router.route(make_request())

        assert result.action == "respond"
        assert result.response is recording.response


# --------------------------------------------------------------------------
# [4] timeout fallback: both pass_through-shaped and respond-shaped results
# --------------------------------------------------------------------------


class TestTimeoutFallback:
    @pytest.mark.asyncio
    async def test_timeout_pass_through_fallback_is_not_recorded(self):
        async def handle_fn(request, on_disconnect):
            return HandlerResult(action="pass_through")

        recorder = FakeRecorder()
        router = build_router(
            config=make_config(mode=Mode.LIVE),
            agent_handler=FakeAgentHandler(handle_fn),
            recorder=recorder,
        )

        result = await router.route(make_request())

        assert result.action == "pass_through"
        assert recorder.saved == []

    @pytest.mark.asyncio
    async def test_timeout_respond_fallback_is_still_recorded(self):
        """Resolved interpretation of plan.md step [5] ("live thanh cong ->
        recorder.save"): any `respond` HandlerResult the live path produces
        gets recorded, whether it came from a genuine agent answer or from
        `timeout_fallback` (return_error/built_in) -- `AgentHandler` gives
        the router no signal to distinguish the two, and recording a
        timeout/fallback response is still useful for replay debugging."""
        fallback_response = make_response(status_code=504, body='{"error": "agent timeout"}')

        async def handle_fn(request, on_disconnect):
            return HandlerResult(action="respond", response=fallback_response)

        recorder = FakeRecorder()
        router = build_router(
            config=make_config(mode=Mode.LIVE),
            agent_handler=FakeAgentHandler(handle_fn),
            recorder=recorder,
        )

        result = await router.route(make_request())

        assert result.action == "respond"
        assert len(recorder.saved) == 1
        assert recorder.saved[0][1] is fallback_response


# --------------------------------------------------------------------------
# client disconnect -> no record (D5)
# --------------------------------------------------------------------------


class TestClientDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_yields_pass_through_and_does_not_record(self):
        async def handle_fn(request, on_disconnect):
            raise ClientDisconnected("client gone")

        recorder = FakeRecorder()
        router = build_router(
            config=make_config(mode=Mode.LIVE),
            agent_handler=FakeAgentHandler(handle_fn),
            recorder=recorder,
        )

        on_disconnect = asyncio.Event()
        result = await router.route(make_request(), on_disconnect)

        assert result.action == "pass_through"
        assert result.response is None
        assert recorder.saved == []


# --------------------------------------------------------------------------
# state snapshot at entry (D6): a mode switch mid-flow must not change the
# outcome of a request already dispatched to the live path.
# --------------------------------------------------------------------------


class TestStateSnapshot:
    @pytest.mark.asyncio
    async def test_mode_switch_mid_flight_does_not_change_in_flight_result(self):
        config = make_config(mode=Mode.LIVE)
        agent_response = make_response(body='{"still": "live"}')

        async def handle_fn(request, on_disconnect):
            # Simulate a concurrent `POST /mock/mode` landing while this
            # request is mid-flight -- must NOT affect this call's outcome.
            await asyncio.sleep(0)
            config.mode = Mode.REPLAY
            return HandlerResult(action="respond", response=agent_response)

        recorder = FakeRecorder()
        router = build_router(
            config=config,
            selector=FakeSelector(selected=True),
            agent_handler=FakeAgentHandler(handle_fn),
            recorder=recorder,
            match_fn=make_match_fn(None),
        )

        result = await router.route(make_request())

        assert config.mode == Mode.REPLAY  # the mutation did happen
        # ...but THIS request still got the live/agent outcome, and was recorded.
        assert result.action == "respond"
        assert result.response is agent_response
        assert len(recorder.saved) == 1


# --------------------------------------------------------------------------
# concurrent append-during-match must never raise (D6) -- exercised against
# the *real* Recorder + matcher (not fakes) since the guarantee comes from
# Recorder.snapshot()'s immutable-tuple contract.
# --------------------------------------------------------------------------


class TestConcurrentAppendDuringMatch:
    @pytest.mark.asyncio
    async def test_concurrent_save_during_match_never_raises(self, tmp_path: Path):
        recorder = Recorder(tmp_path)
        config = make_config(mode=Mode.REPLAY, min_confidence=0.0)
        router = ModeRouter(
            config,
            FakeDomainFilter(intercept=True),
            FakeSelector(selected=False),
            FakeAgentHandler(never_called_handle_fn),
            recorder,
            match_fn=real_match,
            fuzzy_key_fn=real_fuzzy_key,
        )

        async def saver(n: int) -> None:
            for i in range(n):
                await recorder.save(
                    make_request(path=f"/v1/widgets/{i}"), make_response(), source="live"
                )

        async def matcher_caller(n: int) -> None:
            for _ in range(n):
                await router.route(make_request())
                await asyncio.sleep(0)

        # No RuntimeError ("dictionary changed size during iteration" or
        # similar) should ever surface, regardless of interleaving.
        await asyncio.gather(saver(50), matcher_caller(50), saver(50), matcher_caller(50))

        assert len(recorder.list()) == 100
