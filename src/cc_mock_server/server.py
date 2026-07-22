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
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from mitmproxy import http

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
        # action == "pass_through": deliberately do NOT set flow.response so
        # mitmproxy forwards the request upstream and relays the real
        # response back unmodified.

    def error(self, flow: "http.HTTPFlow") -> None:
        """Flow-scoped: fires when this specific flow's connection breaks
        (e.g. mid-await inside `request()`). Best-effort -- a no-op if the
        flow already finished (nothing left in `_disconnect_events`)."""
        event = self._disconnect_events.get(flow.id)
        if event is not None:
            event.set()

    def client_disconnected(self, client: "connection.Client") -> None:
        """Connection-scoped fallback: wakes every flow-scoped Event still
        in flight on this TCP connection."""
        for flow_id in self._client_flows.pop(client.id, ()):
            event = self._disconnect_events.get(flow_id)
            if event is not None:
                event.set()
