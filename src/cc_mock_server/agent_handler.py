"""Agent transport for live-mode requests (plan.md phase 5).

`AgentHandler.handle()` turns an intercepted `Request` into a `HandlerResult`
via exactly one of two transports selected by `config.agent_mode` (D2, never
mixed per request):

- `sync`: `await httpx.AsyncClient.post(agent_url, json=payload)` and use the
  JSON body returned as the response inline. No `asyncio.Future` involved.
- `pending` (default): register an `asyncio.Future` in `self.pending`,
  optionally notify `agent_url` fire-and-forget (its body is never read as
  the response), then `await asyncio.wait_for(future, agent_timeout)`.
  Resolution happens *only* via `respond()` (typically called from the
  control API in phase 7).

Cross-cutting decisions honored here:
- D1: futures are created via `get_running_loop().create_future()`;
  `respond()` accepts an optional `loop` for the cross-thread
  `call_soon_threadsafe` bridge, and always guards `if not future.done()`.
- D3: `agent_url` must be loopback (`config.is_loopback`); headers sent to
  the agent are masked with the same `mask_headers` helper the recorder
  uses, applied *before* the payload is built.
- D5: timeout -> `timeout_fallback`; client disconnect (signalled via the
  `on_disconnect` `asyncio.Event`) cancels the pending future and raises
  `ClientDisconnected` so the caller (phase 6 router) knows not to record;
  `finally` always pops `self.pending` (no leak); `max_pending` caps
  admission with an immediate 503 (no future created); `request_id` is a
  fresh `uuid4` (never content-derived).
- D8: request bodies are already base64-encoded upstream by
  `Request.from_raw` when binary; the payload simply forwards
  `body`/`is_json`/`content_type` as-is.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional

import httpx

from cc_mock_server.config import Config, is_loopback
from cc_mock_server.enums import AgentMode, TimeoutFallback
from cc_mock_server.models import HandlerResult, PendingRequest, Request, Response
from cc_mock_server.sanitize import mask_headers as default_mask_headers

logger = logging.getLogger(__name__)

MaskHeadersFn = Callable[[Mapping[str, str]], dict[str, str]]
SleepFn = Callable[[float], Awaitable[Any]]


class ClientDisconnected(Exception):
    """Raised by `handle()` when `on_disconnect` fires before the agent
    responds. The caller (phase 6 router) must catch this and skip
    recording (D5) — the pending future has already been cancelled and
    popped by the time this propagates."""


def _is_valid_json_text(text: str) -> bool:
    try:
        json.loads(text)
    except (ValueError, TypeError):
        return False
    return True


def _error_response(status_code: int, message: str) -> Response:
    return Response(
        status_code=status_code,
        headers={"content-type": "application/json"},
        body=json.dumps({"error": message}),
        is_json=True,
        content_type="application/json",
    )


class AgentHandler:
    """Sync-XOR-pending agent transport + built-in fallback handler."""

    def __init__(
        self,
        config: Config,
        mask_headers: MaskHeadersFn = default_mask_headers,
        *,
        client: Optional[httpx.AsyncClient] = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        if config.agent_url and not is_loopback(config.agent_url):
            raise ValueError(
                f"agent_url must resolve to a loopback host (D3): {config.agent_url!r}"
            )
        self._config = config
        self._mask_headers = mask_headers
        self._client = client if client is not None else httpx.AsyncClient()
        self._sleep = sleep
        #: id -> in-flight pending request (D5: always popped in `finally`).
        self.pending: dict[str, PendingRequest] = {}
        #: keeps fire-and-forget notify tasks alive until they finish.
        self._background_tasks: set[asyncio.Task] = set()

    async def aclose(self) -> None:
        """Release the underlying `httpx.AsyncClient`."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------

    async def handle(
        self, request: Request, on_disconnect: Optional[asyncio.Event] = None
    ) -> HandlerResult:
        """Dispatch to the sync or pending transport per `config.agent_mode`
        (D2). `on_disconnect`, when provided, is an `asyncio.Event` set by
        the caller (phase 6) when the client connection dies; only
        meaningful in `pending` mode."""
        if self._config.agent_mode == AgentMode.SYNC:
            return await self._handle_sync(request)
        return await self._handle_pending(request, on_disconnect)

    # ------------------------------------------------------------------
    # sync transport
    # ------------------------------------------------------------------

    async def _handle_sync(self, request: Request) -> HandlerResult:
        request_id = str(uuid.uuid4())
        payload = self._build_payload(request_id, request)
        try:
            http_response = await self._client.post(
                self._config.agent_url, json=payload, timeout=self._config.agent_timeout
            )
        except httpx.TimeoutException:
            return self._timeout_fallback(request)
        except httpx.HTTPError as exc:
            logger.warning("sync agent call failed for %s: %s", request_id, exc)
            return self._timeout_fallback(request)

        try:
            data = http_response.json()
        except ValueError:
            logger.warning("agent returned a non-JSON body for %s", request_id)
            return HandlerResult(
                action="respond",
                response=_error_response(502, "agent returned an invalid JSON response"),
            )

        try:
            response = self._response_from_agent_json(data)
        except ValueError as exc:
            logger.warning("agent response shape invalid for %s: %s", request_id, exc)
            return HandlerResult(
                action="respond", response=_error_response(502, "agent response was malformed")
            )
        return HandlerResult(action="respond", response=response)

    # ------------------------------------------------------------------
    # pending transport
    # ------------------------------------------------------------------

    async def _handle_pending(
        self, request: Request, on_disconnect: Optional[asyncio.Event]
    ) -> HandlerResult:
        if len(self.pending) >= self._config.max_pending:
            return HandlerResult(
                action="respond", response=_error_response(503, "max_pending capacity reached")
            )

        request_id = str(uuid.uuid4())  # D5: uuid4, never content-derived
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Response]" = loop.create_future()
        self.pending[request_id] = PendingRequest(
            request_id=request_id,
            request=request,
            future=future,
            created_at=datetime.now(timezone.utc),
        )
        try:
            if self._config.agent_url:
                payload = self._build_payload(request_id, request)
                self._fire_and_forget(self._notify_agent(request_id, payload))

            response = await self._wait_for_future(future, on_disconnect)
            return HandlerResult(action="respond", response=response)
        except asyncio.TimeoutError:
            return self._timeout_fallback(request)
        finally:
            self.pending.pop(request_id, None)  # D5: never leak

    async def _wait_for_future(
        self, future: "asyncio.Future[Response]", on_disconnect: Optional[asyncio.Event]
    ) -> Response:
        """Race `future` against the configured timeout and, if given, a
        client-disconnect signal. Injectable `self._sleep` lets tests use a
        fake clock instead of a real wall-clock timeout (phase-5 success
        criterion)."""
        timeout_task = asyncio.ensure_future(self._sleep(self._config.agent_timeout))
        disconnect_task = (
            asyncio.ensure_future(on_disconnect.wait()) if on_disconnect is not None else None
        )
        waiters: set[asyncio.Future] = {future, timeout_task}
        if disconnect_task is not None:
            waiters.add(disconnect_task)
        try:
            done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if future in done:
                return future.result()
            if disconnect_task is not None and disconnect_task in done:
                future.cancel()
                raise ClientDisconnected(
                    "client disconnected while awaiting agent response"
                )
            raise asyncio.TimeoutError()
        finally:
            for task in (timeout_task, disconnect_task):
                if task is not None and not task.done():
                    task.cancel()
            for task in (timeout_task, disconnect_task):
                if task is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    def _fire_and_forget(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _notify_agent(self, request_id: str, payload: dict[str, Any]) -> None:
        """Fire-and-forget notify (D2): the response body is never read as
        the HandlerResult — resolution happens only via `respond()`."""
        try:
            await self._client.post(
                self._config.agent_url, json=payload, timeout=self._config.agent_timeout
            )
        except httpx.HTTPError as exc:
            logger.warning("agent notify failed for %s: %s", request_id, exc)

    # ------------------------------------------------------------------
    # respond() — the only way a pending future resolves (D2)
    # ------------------------------------------------------------------

    def respond(
        self,
        request_id: str,
        status: int,
        body: Any,
        *,
        headers: Optional[Mapping[str, str]] = None,
        content_type: Optional[str] = None,
        chunks: Optional[list[str]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> bool:
        """Resolve the pending future for `request_id`. Returns False if
        `request_id` is unknown (already resolved/timed out/disconnected,
        or never existed) rather than raising.

        Guarded with `if not future.done()` (D1/D2): a second `respond()`
        for an already-resolved id is a silent no-op, never
        `InvalidStateError`. When `loop` is given (cross-thread control API,
        D1 fallback), the guarded set is scheduled via
        `loop.call_soon_threadsafe`; otherwise it runs inline on the
        current (single) loop.
        """
        pending = self.pending.get(request_id)
        if pending is None:
            return False

        response = self._make_response(status, body, headers, content_type, chunks=chunks)

        def _resolve() -> None:
            if not pending.future.done():
                pending.future.set_result(response)

        if loop is not None:
            loop.call_soon_threadsafe(_resolve)
        else:
            _resolve()
        return True

    # ------------------------------------------------------------------
    # timeout fallback + built-in handler
    # ------------------------------------------------------------------

    def _timeout_fallback(self, request: Request) -> HandlerResult:
        fallback = self._config.timeout_fallback
        if fallback == TimeoutFallback.RETURN_ERROR:
            return HandlerResult(action="respond", response=_error_response(504, "agent timeout"))
        if fallback == TimeoutFallback.PASS_THROUGH:
            return HandlerResult(action="pass_through")
        if fallback == TimeoutFallback.BUILT_IN:
            return HandlerResult(action="respond", response=self.built_in_response(request))
        raise AssertionError(f"unhandled timeout_fallback: {fallback!r}")  # pragma: no cover

    def built_in_response(self, request: Request) -> Response:
        """Built-in handler contract: `200 {}`, or an echo of the request
        body when it is JSON, always `Content-Type: application/json`."""
        body = request.body if (request.is_json and request.body) else "{}"
        return Response(
            status_code=200,
            headers={"content-type": "application/json"},
            body=body,
            is_json=True,
            content_type="application/json",
        )

    # ------------------------------------------------------------------
    # payload / response shaping
    # ------------------------------------------------------------------

    def _build_payload(self, request_id: str, request: Request) -> dict[str, Any]:
        """Payload sent to the agent (no `context` field — D-clarify).
        Headers are masked *before* this dict is built (D3); binary bodies
        arrive already base64-encoded via `Request.from_raw` (D8)."""
        return {
            "request_id": request_id,
            "method": request.method,
            "url": request.url,
            "headers": self._mask_headers(request.headers),
            "body": request.body,
            "is_json": request.is_json,
            "content_type": request.content_type,
        }

    def _response_from_agent_json(self, data: Any) -> Response:
        if not isinstance(data, dict):
            raise ValueError("agent JSON response must be an object")
        status_code = data.get("status_code", data.get("status", 200))
        # D10 phase 9 (agent-composed SSE, direction B): `chunks` (a list of
        # pre-framed SSE event strings) is the authoritative streaming
        # signal; `stream: true` without `chunks` is an explicit mistake.
        chunks = data.get("chunks")
        if data.get("stream") and chunks is None:
            raise ValueError("a stream response requires a 'chunks' list")
        return self._make_response(
            status_code,
            data.get("body", {}),
            data.get("headers"),
            data.get("content_type"),
            chunks=chunks,
        )

    def _make_response(
        self,
        status_code: int,
        body: Any,
        headers: Optional[Mapping[str, str]],
        content_type: Optional[str],
        *,
        chunks: Optional[list[str]] = None,
    ) -> Response:
        # D10 phase 9: an agent-composed SSE stream -- `chunks` present (a
        # list of pre-framed event strings) short-circuits the body path
        # entirely; cc-mock joins them verbatim (agent-agnostic, direction B).
        if chunks is not None:
            if not isinstance(chunks, list) or not all(isinstance(item, str) for item in chunks):
                raise ValueError("stream 'chunks' must be a list of strings")
            return Response.from_chunks(
                chunks,
                status_code=int(status_code),
                headers=dict(headers or {}),
                content_type=content_type,
            )

        headers_dict = dict(headers or {})

        if body is None:
            text, is_json = "", False
        elif isinstance(body, (dict, list)):
            text, is_json = json.dumps(body), True
            content_type = content_type or "application/json"
        elif isinstance(body, str):
            text = body
            is_json = _is_valid_json_text(text)
            if content_type is None and is_json:
                content_type = "application/json"
        else:
            text, is_json = json.dumps(body), True
            content_type = content_type or "application/json"

        if content_type and not any(key.lower() == "content-type" for key in headers_dict):
            headers_dict["content-type"] = content_type

        return Response(
            status_code=int(status_code),
            headers=headers_dict,
            body=text,
            is_json=is_json,
            content_type=content_type,
        )
