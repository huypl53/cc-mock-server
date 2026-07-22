"""Mode router: the pipeline that ties filter/selector/matcher/agent/recorder
together for a single intercepted HTTP request (plan.md phase 6, D6).

`ModeRouter.route()` is the single entry point the proxy addon (`server.py`)
calls per-request. It implements the pipeline priority from plan.md:

```
[0] domain filter (defense-in-depth for plain-HTTP flows that never went
    through the CONNECT/tls_clienthello stage, and for router-level unit
    tests that exercise this branch without a real mitmproxy) -> pass_through
[1] snapshot mode + selector.is_selected(request) once at entry (D6): a
    `POST /mock/mode` (or select/deselect) call that lands while this
    request is still in flight must NOT change its outcome.
[2] mode == replay -> matcher.match (confidence-gated, D4); hit -> the
    recording's response; miss -> replay_miss_strategy.
[3] mode == live: selected -> agent_handler.handle; not selected -> the
    same replay-match-then-miss-strategy path as [2] (so unselected
    endpoints in live mode still serve prior recordings before falling
    back). This is the resolved reading of plan.md's terse "else
    replay/pass_through" -- see phase-06 completion report for the
    rationale.
[4] agent timeout -> the handler already applied timeout_fallback and
    returned a HandlerResult; client disconnect -> `ClientDisconnected` is
    caught here and turned into a pass_through HandlerResult (D5: no
    recording, no response is attempted for an already-gone client).
[5] a `respond` HandlerResult coming out of the live path is recorded via
    `recorder.save` (masked, D3), then its `fuzzy_key` is backfilled (H4)
    using the injected `fuzzy_key_fn` -- this is the composition root's
    resolution of the recorder<->matcher circular import, done by mutating
    the in-memory `Recording.metadata.fuzzy_key` in place after save()
    returns (recorder.py exposes no update-in-place API, so the on-disk
    file keeps `fuzzy_key: null`; only the in-memory copy -- the one
    `match()`/`snapshot()` actually see -- is backfilled).
```

`match()` itself doesn't even read `metadata.fuzzy_key` (it recomputes the
grouping key fresh from `recording.request` every call, per matcher.py), so
this backfill is purely informational/for future tooling, not required for
correct replay behavior.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Iterable, Mapping, Optional

from cc_mock_server import streaming
from cc_mock_server.agent_handler import AgentHandler, ClientDisconnected
from cc_mock_server.config import Config
from cc_mock_server.enums import Mode, ReplayMissStrategy
from cc_mock_server.filter import DomainFilter
from cc_mock_server.matcher import MatchResult
from cc_mock_server.matcher import fuzzy_key as default_fuzzy_key
from cc_mock_server.matcher import match as default_match
from cc_mock_server.models import HandlerResult, Recording, Request, Response
from cc_mock_server.recorder import Recorder
from cc_mock_server.selector import EndpointSelector

logger = logging.getLogger(__name__)

MatchFn = Callable[[Request, Iterable[Recording], float], Optional[MatchResult]]
FuzzyKeyFn = Callable[[Request], str]


def _error_response(status_code: int, message: str) -> Response:
    return Response(
        status_code=status_code,
        headers={"content-type": "application/json"},
        body=json.dumps({"error": message}),
        is_json=True,
        content_type="application/json",
    )


class ModeRouter:
    """Wires `DomainFilter` + `EndpointSelector` + `AgentHandler` +
    `Recorder` + the fuzzy matcher into the single per-request pipeline
    (D6). Dependencies are injected (not imported ad hoc) so unit tests can
    substitute fakes/mocks for every one of them."""

    def __init__(
        self,
        config: Config,
        domain_filter: DomainFilter,
        selector: EndpointSelector,
        agent_handler: AgentHandler,
        recorder: Recorder,
        *,
        match_fn: MatchFn = default_match,
        fuzzy_key_fn: FuzzyKeyFn = default_fuzzy_key,
    ) -> None:
        self._config = config
        self._domain_filter = domain_filter
        self._selector = selector
        self._agent_handler = agent_handler
        self._recorder = recorder
        self._match_fn = match_fn
        self._fuzzy_key_fn = fuzzy_key_fn

    @property
    def config(self) -> Config:
        """Exposed so `server.py`'s `MockAddon` (which only receives
        `router` + `domain_filter`, D1's composition root in `app.py` is
        not itself touched by phase 8) can read `capture_streams` without
        a second config instance."""
        return self._config

    async def route(
        self, request: Request, on_disconnect: Optional[asyncio.Event] = None
    ) -> HandlerResult:
        """Run the full pipeline for one intercepted request. Never raises
        for expected control-flow (disconnect/timeout/miss); those are all
        represented as `HandlerResult` variants."""
        # [0] domain filter -- defense-in-depth / plain-HTTP path (D7).
        if not self._domain_filter.should_intercept(request.host):
            return HandlerResult(action="pass_through")

        # [1] snapshot mode + selected decision at entry (D6): read once,
        # into local variables, so a concurrent `POST /mock/mode` (mutating
        # `self._config.mode`) or select/deselect (mutating selector state)
        # cannot change the outcome of a request already in flight.
        mode = self._config.mode
        selected = self._selector.is_selected(request)

        if mode == Mode.LIVE and selected:
            return await self._handle_live(request, on_disconnect)

        # mode == replay, OR mode == live but this endpoint isn't selected
        # for the agent: try a fuzzy-matched recording first ([2] / [3]).
        hit = self._match_and_respond(request)
        if hit is not None:
            return hit
        return await self._handle_miss(request, on_disconnect)

    # ------------------------------------------------------------------
    # replay lookup (shared by mode==replay and mode==live+unselected)
    # ------------------------------------------------------------------

    def _match_and_respond(self, request: Request) -> Optional[HandlerResult]:
        # D6: iterate a snapshot/copy, never the live mutable collection --
        # `Recorder.snapshot()` already returns an immutable tuple copy, so
        # a concurrent `recorder.save()` can never race this iteration.
        candidates = self._recorder.snapshot()
        result = self._match_fn(request, candidates, self._config.min_confidence)
        if result is None:
            return None
        return HandlerResult(action="respond", response=result.recording.response)

    async def _handle_miss(
        self, request: Request, on_disconnect: Optional[asyncio.Event]
    ) -> HandlerResult:
        strategy = self._config.replay_miss_strategy
        if strategy == ReplayMissStrategy.PASS_THROUGH:
            return HandlerResult(action="pass_through")
        if strategy == ReplayMissStrategy.RETURN_ERROR:
            return HandlerResult(
                action="respond", response=_error_response(502, "no matching recording")
            )
        if strategy == ReplayMissStrategy.LIVE:
            return await self._handle_live(request, on_disconnect)
        raise AssertionError(f"unhandled replay_miss_strategy: {strategy!r}")  # pragma: no cover

    # ------------------------------------------------------------------
    # live agent dispatch + recording (D5/D6)
    # ------------------------------------------------------------------

    async def _handle_live(
        self, request: Request, on_disconnect: Optional[asyncio.Event]
    ) -> HandlerResult:
        try:
            result = await self._agent_handler.handle(request, on_disconnect)
        except ClientDisconnected:
            # D5: client already gone -- never record, never try to respond.
            logger.info("client disconnected before agent responded for %s %s", request.method, request.url)
            return HandlerResult(action="pass_through")

        if result.action == "respond":
            assert result.response is not None
            await self._save_recording(request, result.response)
        return result

    async def _save_recording(self, request: Request, response: Response) -> None:
        recording = await self._recorder.save(request, response, source="live")
        # H4 backfill: `Recorder.save` always writes `fuzzy_key=None` to
        # avoid a circular import with matcher.py. Mutating the returned
        # (in-memory) `Recording` in place here reaches the same object
        # `Recorder` keeps in its internal dict (Python reference
        # semantics), so `recorder.list()`/`snapshot()` reflect the
        # backfilled key from this point on.
        recording.metadata.fuzzy_key = self._fuzzy_key_fn(request)

    # ------------------------------------------------------------------
    # streaming / SSE (D10, phase 8): pass-through TEE capture, NOT the
    # pending/agent path above -- pending holds the connection open
    # waiting for an agent reply, which is fundamentally incompatible with
    # a long-lived stream; pass-through instead lets the app hit the real
    # upstream once, `server.py` tees the bytes to the client while
    # buffering them, and this module records the result afterwards.
    # ------------------------------------------------------------------

    def should_capture_stream(self, response_headers: Mapping[str, str]) -> bool:
        """Should `server.py` tee-capture this pass-through response?

        True only when `capture_streams` is enabled AND the real
        upstream's response headers say `text/event-stream` (D10). Reading
        `self._config.capture_streams` here (rather than `server.py`
        reading `Config` directly) keeps the "what counts as a capturable
        stream" decision in one place, next to the rest of the routing
        policy.
        """
        return self._config.capture_streams and streaming.is_sse(response_headers)

    async def save_stream_recording(self, request: Request, response: Response) -> None:
        """Persist a captured pass-through SSE exchange (D10).

        Mirrors `_handle_live`'s `_save_recording` (masking + fuzzy_key
        backfill) exactly -- the only difference is which pipeline branch
        calls it: this one is reached from `server.py`'s `response()` hook
        once an SSE body has fully relayed through pass-through, never from
        the live-agent/pending path.
        """
        await self._save_recording(request, response)
