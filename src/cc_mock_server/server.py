"""mitmproxy addon: CONNECT/TLS-stage domain filter (D7) + HTTP-stage
`ModeRouter` dispatch (plan.md phase 6).

`MockAddon` is the only place that talks to mitmproxy's flow objects
directly -- everything else in this codebase only knows about
`cc_mock_server.models.Request`/`Response`. Two hook stages are used:

- `tls_clienthello`: the CONNECT/TLS-stage filter (D7). A domain outside
  the configured filter gets `data.ignore_connection = True`, which makes
  mitmproxy tunnel the raw TLS bytes through unmodified -- no certificate
  is generated for that host, no cert-pinning is broken, and the traffic
  is never decrypted. This is the *only* thing that can prevent TLS
  termination; the plain-HTTP fallback below cannot un-terminate a
  connection that was never encrypted in the first place.
- `request`/`error`/`client_disconnected`: the HTTP-stage pipeline. `error`
  is flow-scoped (fires when a specific flow's connection breaks while our
  `request()` coroutine may still be awaiting the router) and
  `client_disconnected` is connection-scoped (fires once per TCP
  connection, which may have carried several flows on keep-alive) -- both
  are wired to the same per-flow `asyncio.Event` so `ModeRouter`/
  `AgentHandler`'s `on_disconnect` plumbing (D5) sees either signal.

Plain HTTP (no CONNECT at all, e.g. `http://` absolute-form requests) never
triggers `tls_clienthello`, so `ModeRouter.route()` re-checks the domain
filter as pipeline step [0] -- that's the "defense-in-depth" mentioned in
router.py.

Streaming / SSE capture-on-pass-through (D10, phase 8)
-------------------------------------------------------
Three more mitmproxy hooks are used for this, all scoped to flows the
router already decided are `pass_through` (never the live-agent/pending
path -- see router.py's `save_stream_recording` docstring for why):

- `running()`: sets the *global* mitmproxy option `store_streamed_bodies =
  True` once, at startup. This is a process-wide switch, but it is inert
  for every flow that never sets `flow.response.stream` truthy -- and only
  the flows marked below ever do that -- so it never changes behavior for
  non-streamed or non-captured traffic.
- `responseheaders(flow)`: fires once the real upstream's response
  headers arrive, before any body bytes. For a flow `request()` marked as
  a capture candidate (pass-through + `capture_streams` enabled), this
  checks `streaming.is_sse(...)`; if it matches, setting
  `flow.response.stream = True` is what makes mitmproxy relay each
  upstream chunk to the client the instant it arrives (preserving TTFT)
  instead of buffering the full body before forwarding anything --
  `store_streamed_bodies` (above) is what *additionally* makes the
  fully-relayed bytes available in `flow.response.content` once the
  stream completes, so this module never has to maintain its own
  duplicate buffer.
- `response(flow)`: an async hook that fires only after
  `ResponseEndOfMessage` for a streamed response (see
  `mitmproxy/proxy/layers/http/__init__.py`'s `state_stream_response_body`
  -> `send_response(already_streamed=True)`) -- i.e. only once the tee'd
  stream has *fully* relayed to the client. This is also exactly the "no
  partial recording on disconnect" requirement (D5, reused without new
  plumbing): if the client disconnects mid-stream, this hook simply never
  fires for that flow, so `save_stream_recording` is never called.

Documented blocker -- why `stream_delay` cannot pace *replay*: a response
`request()` sets directly on `flow` (both a live-agent `respond` and a
replay hit go through this) is an "early"/injected response. mitmproxy's
own state machine (`HttpConnection.state_consume_request_body`'s
early-response branch, `mitmproxy/proxy/layers/http/__init__.py` ~L380-387)
unconditionally calls the *non-streamed* `send_response()` for such a
response -- it never even inspects `flow.response.stream`. Inside
`send_response()` (~L494-526), the `HttpResponseHook` (`response()`) fires
*before* any bytes reach the client, and when `already_streamed=False` the
entire `raw_content` is sent as a single `ResponseData` chunk -- there is
no yield point between "events" to delay between. Reproducing real
per-event pacing for a replayed/synthetic body would require bypassing
mitmproxy's HTTP layer entirely (hand-rolled socket writes) -- exactly the
kind of low-level plumbing/rabbit-hole D10 forbids. `stream_delay` is
therefore accepted, validated (`config.py`), and threaded through the CLI
for forward-compatibility, but today it has no observable effect on
replay: the full SSE body is emitted in one frame with correct framing and
`Content-Type` (content-correct fallback, matching the HTTPS integration
test's documented-blocker precedent in phase 6).

One more wrinkle captured bodies introduce: mitmproxy preserves framing
headers like `Transfer-Encoding` on `flow.response.headers` verbatim (it
validates them, never strips them). `_strip_hop_by_hop_headers` below
removes those before a captured response is ever recorded, since
`apply_response_to_flow`'s `http.Response.make(...)` always re-derives a
fresh, correct `Content-Length` from the raw body on replay -- a stale
`Transfer-Encoding: chunked` sitting alongside that would describe framing
that no longer matches the (now non-chunked) replayed bytes.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from mitmproxy import ctx, http

from cc_mock_server import streaming
from cc_mock_server.models import Request, Response
from cc_mock_server.router import ModeRouter

if TYPE_CHECKING:  # pragma: no cover
    from mitmproxy import connection, tls

logger = logging.getLogger(__name__)


def _multidict_to_dict(items: object) -> dict[str, str]:
    """Collapse a mitmproxy multidict's `.items(multi=True)` pairs into a
    plain dict, keeping the *last* value per key (matches
    `models.Request`'s documented last-value-wins semantics -- mitmproxy's
    own single-value `.items()`/`[]` keep the *first* value instead, which
    would silently disagree with our model contract)."""
    result: dict[str, str] = {}
    for key, value in items:  # type: ignore[misc]
        result[key] = value
    return result


#: Per-hop framing headers (RFC 9110 §7.6.1) that must never be captured
#: into a recording (D10): mitmproxy preserves e.g. `Transfer-Encoding` on
#: `flow.response.headers` verbatim (it validates but does not strip it),
#: but `apply_response_to_flow`'s `http.Response.make(...)` always
#: re-derives fresh framing (a correct `Content-Length`) from whatever raw
#: body it's given on replay -- keeping a stale `Transfer-Encoding: chunked`
#: alongside that freshly computed length would send a malformed response
#: (both headers present) even though the body is no longer chunk-framed.
_HOP_BY_HOP_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "connection", "keep-alive", "proxy-connection"}
)


def _strip_hop_by_hop_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in _HOP_BY_HOP_HEADERS}


def request_from_flow(flow: "http.HTTPFlow") -> Request:
    """Convert an intercepted mitmproxy `HTTPFlow` into a `models.Request`.

    Uses `req.content` (decoded/decompressed per any `Content-Encoding`)
    rather than `req.raw_content` so `Request.from_raw`'s D8 content-type
    classification and the matcher's JSON body comparison operate on the
    logical body, not on-the-wire compressed bytes. Falls back to
    `raw_content` if decompression fails (invalid/unsupported encoding)
    rather than raising and dropping the flow.
    """
    req = flow.request
    try:
        raw_body = req.content
    except ValueError:
        raw_body = req.raw_content
    raw_body = raw_body or b""

    content_type = req.headers.get("content-type")
    path_only = req.path.split("?", 1)[0] or "/"

    return Request.from_raw(
        method=req.method,
        url=req.url,
        host=req.host,
        path=path_only,
        query=_multidict_to_dict(req.query.items(multi=True)),
        headers=_multidict_to_dict(req.headers.items(multi=True)),
        raw_body=raw_body,
        content_type=content_type,
    )


def apply_response_to_flow(flow: "http.HTTPFlow", response: Response) -> None:
    """Set `flow.response` from a `models.Response`.

    `http.Response.make()`'s content setter re-derives `Content-Length`
    (and re-applies `Content-Encoding` compression, if that header is
    present) from the decoded body we hand it -- exactly undoing the
    decompression `request_from_flow`/response-capture did, so replayed
    bytes are wire-correct even for a recording captured from a
    gzip-compressed upstream response.
    """
    raw_body = response.decoded_body()
    flow.response = http.Response.make(response.status_code, raw_body, dict(response.headers))


class MockAddon:
    """The mitmproxy addon: CONNECT/TLS filter (D7) + HTTP router dispatch."""

    def __init__(self, router: ModeRouter, domain_filter) -> None:
        self._router = router
        self._domain_filter = domain_filter
        #: flow.id -> the Event `ModeRouter`/`AgentHandler` await on for
        #: client-disconnect detection (D5). Always popped in `request()`'s
        #: `finally` -- never leaked across requests.
        self._disconnect_events: dict[str, asyncio.Event] = {}
        #: client_conn.id -> set of flow ids currently in flight on that
        #: connection, so a connection-scoped `client_disconnected` can
        #: wake every flow-scoped Event for that connection (keep-alive may
        #: carry several flows per TCP connection).
        self._client_flows: dict[str, set[str]] = defaultdict(set)
        #: flow.id -> the `Request` for a pass-through flow that MIGHT turn
        #: out to be an SSE stream worth tee-capturing (D10). Populated in
        #: `request()` only when the router chose pass_through AND
        #: `capture_streams` is enabled; popped (without saving) in
        #: `responseheaders()` the moment the real response headers prove
        #: it isn't SSE, or (with saving) in `response()` once a streamed
        #: SSE body has fully relayed. Also popped on error/disconnect so
        #: it never leaks across requests.
        self._stream_capture: dict[str, Request] = {}

    # ------------------------------------------------------------------
    # startup
    # ------------------------------------------------------------------

    def running(self) -> None:
        """Enable mitmproxy's own streamed-body storage globally (D10).

        This is a process-wide mitmproxy option, but it only has an effect
        for a flow whose `flow.response.stream` is itself set truthy --
        which `responseheaders()` below only ever does for a flow this
        addon already marked as a capture candidate. Every other flow
        (including all non-SSE pass-through traffic) is completely
        unaffected, which is what keeps this phase from regressing
        existing pass-through behavior.
        """
        ctx.options.update(store_streamed_bodies=True)

    # ------------------------------------------------------------------
    # CONNECT/TLS stage (D7)
    # ------------------------------------------------------------------

    def tls_clienthello(self, data: "tls.ClientHelloData") -> None:
        """Domains outside the filter are never TLS-terminated (D7): no
        certificate is generated, so cert-pinned clients keep working and
        no CA trust is required for that host."""
        host = None
        server_address = data.context.server.address
        if server_address:
            host = server_address[0]
        if not host:
            host = data.client_hello.sni
        if host and not self._domain_filter.should_intercept(host):
            data.ignore_connection = True

    # ------------------------------------------------------------------
    # HTTP stage
    # ------------------------------------------------------------------

    async def request(self, flow: "http.HTTPFlow") -> None:
        request = request_from_flow(flow)

        on_disconnect = asyncio.Event()
        client_id = flow.client_conn.id
        self._disconnect_events[flow.id] = on_disconnect
        self._client_flows[client_id].add(flow.id)
        try:
            result = await self._router.route(request, on_disconnect)
        finally:
            self._disconnect_events.pop(flow.id, None)
            flows = self._client_flows.get(client_id)
            if flows is not None:
                flows.discard(flow.id)
                if not flows:
                    self._client_flows.pop(client_id, None)

        if result.action == "respond":
            assert result.response is not None
            apply_response_to_flow(flow, result.response)
        elif self._router.config.capture_streams and self._domain_filter.should_intercept(
            request.host
        ):
            # pass_through: deliberately do NOT set flow.response so
            # mitmproxy forwards the request upstream and relays the real
            # response back unmodified -- but remember the request in case
            # the real response turns out to be SSE worth tee-capturing
            # (D10). Gated on `should_intercept` too so a domain the user
            # explicitly excluded from the filter is never captured, same
            # as it's never intercepted/recorded any other way (D7).
            # `responseheaders()`/`response()` below do the rest; this dict
            # entry is always eventually popped there (or in
            # `error()`/`client_disconnected()`), never leaked.
            self._stream_capture[flow.id] = request

    def error(self, flow: "http.HTTPFlow") -> None:
        """Flow-scoped: fires when this specific flow's connection breaks
        (e.g. mid-await inside `request()`). Best-effort -- a no-op if the
        flow already finished (nothing left in `_disconnect_events`)."""
        event = self._disconnect_events.get(flow.id)
        if event is not None:
            event.set()
        # D10/D5: a broken flow must never produce a (partial) recording.
        self._stream_capture.pop(flow.id, None)

    def client_disconnected(self, client: "connection.Client") -> None:
        """Connection-scoped fallback: wakes every flow-scoped Event still
        in flight on this TCP connection."""
        for flow_id in self._client_flows.pop(client.id, ()):
            event = self._disconnect_events.get(flow_id)
            if event is not None:
                event.set()
            # D10/D5: same "no partial recording" rule as error() above.
            self._stream_capture.pop(flow_id, None)

    # ------------------------------------------------------------------
    # streaming / SSE capture-on-pass-through (D10, phase 8)
    # ------------------------------------------------------------------

    def responseheaders(self, flow: "http.HTTPFlow") -> None:
        """Headers-stage decision: is this pass-through response actually
        SSE? Must run here (before any body bytes arrive) -- setting
        `flow.response.stream = True` at this stage is what makes
        mitmproxy tee each upstream chunk to the client as it arrives
        (see this module's docstring) rather than buffering the full body
        first."""
        request = self._stream_capture.get(flow.id)
        if request is None or flow.response is None:
            return
        headers = _multidict_to_dict(flow.response.headers.items(multi=True))
        if streaming.is_sse(headers):
            flow.response.stream = True
        else:
            # Not SSE after all -- leave pass-through exactly as it was
            # before phase 8 (no capture, no behavior change).
            self._stream_capture.pop(flow.id, None)

    async def response(self, flow: "http.HTTPFlow") -> None:
        """Persist a captured pass-through SSE exchange, once it has fully
        relayed (D10). Only reached for a flow `responseheaders()` above
        confirmed is SSE; never fires at all for a flow whose client
        disconnected before the upstream stream completed (mitmproxy only
        calls this hook after `ResponseEndOfMessage` for a streamed
        response), which is exactly the "no partial recording" guarantee
        this phase needs -- with no extra plumbing."""
        request = self._stream_capture.pop(flow.id, None)
        if request is None or flow.response is None:
            return
        headers = _multidict_to_dict(flow.response.headers.items(multi=True))
        if not streaming.is_sse(headers):
            return  # pragma: no cover -- responseheaders() already filters this
        try:
            raw_body = flow.response.content
        except ValueError:
            raw_body = flow.response.raw_content
        if raw_body is None:
            return  # stream never actually completed -- nothing to save
        content_type = flow.response.headers.get("content-type") or "text/event-stream"
        response = Response.from_raw(
            status_code=flow.response.status_code,
            headers=_strip_hop_by_hop_headers(headers),
            raw_body=raw_body,
            content_type=content_type,
            is_stream=True,
        )
        await self._router.save_stream_recording(request, response)
