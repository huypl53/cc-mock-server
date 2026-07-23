"""Control API (plan.md phase 7, Global AC #10).

`create_control_api(application)` builds a FastAPI app that reads and
mutates the exact same `Application` instances the running proxy uses
(`app.py`'s composition root, D6) -- no second construction, no shadow
state. It is meant to be bound to `config.control_bind` (127.0.0.1 by
default, D3) and served on the SAME event loop as the mitmproxy `Master`
(D1); every route handler here is `async def` so it always runs as a task
on that one loop, meaning `AgentHandler.respond(...)` never needs the
`loop=` cross-thread fallback in the primary deployment path (`cli.py`'s
`start` command appends `uvicorn.Server.serve()` to the same
`asyncio.gather(...)` as `master.run()`).

Endpoints (all under `/mock/*`):
- `GET  /mock/status`            -- snapshot of mode/agent/filter/counts.
- `POST /mock/mode`              -- switch `config.mode` (live/replay).
- `GET  /mock/filter`            -- current filter mode + domain list.
- `POST /mock/filter`            -- add/remove a domain (D7).
- `GET  /mock/select`            -- current selector overrides.
- `POST /mock/select`            -- explicitly select a pattern.
- `DELETE /mock/select/{pattern}`-- explicitly deselect a pattern.
- `GET  /mock/pending`           -- in-flight `agent_mode=pending` requests.
- `POST /mock/respond`           -- resolve a pending future (D1/D2).
- `GET  /mock/recordings`        -- list persisted recordings.
- `DELETE /mock/recordings/{id}` -- delete one recording (id = filename stem).
- `POST /mock/config`            -- partial runtime config update; rejects
  a non-loopback `agent_url` with 400 (D3).
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from cc_mock_server.app import Application
from cc_mock_server.config import is_loopback
from cc_mock_server.enums import AgentMode, Mode, ReplayMissStrategy, TimeoutFallback
from cc_mock_server.sanitize import mask_headers

# ----------------------------------------------------------------------------
# request bodies
# ----------------------------------------------------------------------------


class ModeRequest(BaseModel):
    mode: Mode


class FilterRequest(BaseModel):
    action: Literal["add", "remove"]
    domain: str


class SelectRequest(BaseModel):
    pattern: str


class RespondRequest(BaseModel):
    #: Optional: when omitted, `/mock/respond` auto-targets the single
    #: in-flight pending request (the common case -- the app is blocked on
    #: exactly one call), so an agent never has to thread an id from
    #: `pending` into `respond` through a shell variable. Ambiguous (>1
    #: pending) -> 409 listing the ids so the caller can be explicit.
    request_id: Optional[str] = None
    status: int = 200
    body: Any = None
    headers: Optional[dict[str, str]] = None
    content_type: Optional[str] = None
    #: D10 phase 9 (agent-composed SSE, direction B): when `chunks` is
    #: present (a list of pre-framed SSE event strings) the response is a
    #: `text/event-stream` built by joining them verbatim; `body` is ignored.
    #: `stream` is an explicit intent flag -- true without `chunks` is a 400.
    stream: bool = False
    chunks: Optional[list[str]] = None


class ConfigUpdateRequest(BaseModel):
    """Partial update -- only fields explicitly present in the JSON body are
    applied (`exclude_unset=True`), everything else is left untouched."""

    agent_url: Optional[str] = None
    agent_mode: Optional[AgentMode] = None
    agent_timeout: Optional[float] = None
    timeout_fallback: Optional[TimeoutFallback] = None
    replay_miss_strategy: Optional[ReplayMissStrategy] = None
    min_confidence: Optional[float] = None
    max_pending: Optional[int] = None


def _selector_overrides_snapshot(selector: Any) -> dict[str, dict[str, bool]]:
    """`EndpointSelector` (phase 4, read-only for this phase) exposes no
    public accessor for its explicit overrides -- only `is_selected()`,
    which answers "is this one request selected", not "what's the current
    override set". Reading the private dicts here (copied, never mutated)
    is the only way to serve `GET /mock/select` without modifying
    `selector.py`."""
    return {
        "domain": dict(selector._domain_overrides),
        "method_path": dict(selector._method_path_overrides),
    }


def create_control_api(application: Application) -> FastAPI:
    """Build the control API FastAPI app bound to `application`'s live
    component instances (D6). Call once per running `Application`."""
    api = FastAPI(title="cc-mock-server control API")

    # ------------------------------------------------------------------
    # status / mode
    # ------------------------------------------------------------------

    @api.get("/mock/status")
    async def get_status() -> dict[str, Any]:
        config = application.config
        return {
            "mode": config.mode.value,
            "agent_mode": config.agent_mode.value,
            "agent_url": config.agent_url,
            "agent_timeout": config.agent_timeout,
            "timeout_fallback": config.timeout_fallback.value,
            "replay_miss_strategy": config.replay_miss_strategy.value,
            "min_confidence": config.min_confidence,
            "max_pending": config.max_pending,
            "proxy_port": config.proxy_port,
            "control_port": config.control_port,
            "pending_count": len(application.agent_handler.pending),
            "recordings_count": len(application.recorder.list()),
            "filter_mode": application.domain_filter.mode.value,
            "filter_domains": application.domain_filter.list_domains(),
        }

    @api.post("/mock/mode")
    async def set_mode(body: ModeRequest) -> dict[str, str]:
        application.config.mode = body.mode
        return {"mode": application.config.mode.value}

    # ------------------------------------------------------------------
    # filter (D7)
    # ------------------------------------------------------------------

    @api.get("/mock/filter")
    async def get_filter() -> dict[str, Any]:
        return {
            "mode": application.domain_filter.mode.value,
            "domains": application.domain_filter.list_domains(),
        }

    @api.post("/mock/filter")
    async def update_filter(body: FilterRequest) -> dict[str, Any]:
        if body.action == "add":
            await application.domain_filter.add_domain(body.domain)
        else:
            await application.domain_filter.remove_domain(body.domain)
        return {
            "mode": application.domain_filter.mode.value,
            "domains": application.domain_filter.list_domains(),
        }

    # ------------------------------------------------------------------
    # select / deselect
    # ------------------------------------------------------------------

    @api.get("/mock/select")
    async def get_select() -> dict[str, Any]:
        return {
            "auto_select_filtered": application.selector.auto_select_filtered,
            "overrides": _selector_overrides_snapshot(application.selector),
        }

    @api.post("/mock/select")
    async def select_pattern(body: SelectRequest) -> dict[str, str]:
        await application.selector.select(body.pattern)
        return {"selected": body.pattern}

    @api.delete("/mock/select/{pattern:path}")
    async def deselect_pattern(pattern: str) -> dict[str, str]:
        await application.selector.deselect(pattern)
        return {"deselected": pattern}

    # ------------------------------------------------------------------
    # pending / respond (D1/D2 -- the agent-facing poll->respond loop)
    # ------------------------------------------------------------------

    @api.get("/mock/pending")
    async def list_pending() -> dict[str, Any]:
        # The agent polls this endpoint, so it is a D3 secret-egress path
        # exactly like the callback payload: mask sensitive headers before
        # the request reaches the agent (never leak Authorization/cookie/etc).
        def _dump(pending: Any) -> dict[str, Any]:
            request = pending.request.model_dump()
            request["headers"] = mask_headers(request.get("headers", {}))
            return {
                "request_id": pending.request_id,
                "request": request,
                "created_at": pending.created_at.isoformat(),
            }

        return {
            "pending": [
                _dump(pending)
                for pending in application.agent_handler.pending.values()
            ]
        }

    @api.post("/mock/respond")
    async def respond(body: RespondRequest) -> dict[str, Any]:
        # No `loop=` argument: this handler already runs as a task on the
        # SAME event loop as every pending future (D1) -- the `loop=`
        # cross-thread bridge documented on `AgentHandler.respond` is only
        # for a future where uvicorn is forced onto a separate thread.
        if body.stream and body.chunks is None:
            raise HTTPException(
                status_code=400, detail="a stream response requires a 'chunks' list"
            )
        request_id = body.request_id
        if request_id is None:
            # Auto-target the sole pending request so the caller never has to
            # capture an id from `pending` into a shell variable (fragile in
            # some agent harnesses). Snapshot keys once -- this handler runs
            # on the same loop as the pending dict (D1), no lock needed.
            pending_ids = list(application.agent_handler.pending.keys())
            if not pending_ids:
                raise HTTPException(status_code=404, detail="no pending requests to respond to")
            if len(pending_ids) > 1:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{len(pending_ids)} pending requests; pass request_id explicitly. "
                        f"pending ids: {pending_ids}"
                    ),
                )
            request_id = pending_ids[0]

        resolved = application.agent_handler.respond(
            request_id,
            body.status,
            body.body,
            headers=body.headers,
            content_type=body.content_type,
            chunks=body.chunks,
        )
        if not resolved:
            raise HTTPException(
                status_code=404,
                detail=f"unknown or already-resolved request_id: {request_id!r}",
            )
        return {"request_id": request_id, "resolved": True}

    # ------------------------------------------------------------------
    # recordings
    # ------------------------------------------------------------------

    @api.get("/mock/recordings")
    async def list_recordings() -> dict[str, Any]:
        return {"recordings": [recording.model_dump() for recording in application.recorder.list()]}

    @api.delete("/mock/recordings/{recording_id}")
    async def delete_recording(recording_id: str) -> dict[str, Any]:
        deleted = await application.recorder.delete(recording_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"recording not found: {recording_id!r}")
        return {"deleted": recording_id}

    # ------------------------------------------------------------------
    # config (D3: agent_url must stay loopback)
    # ------------------------------------------------------------------

    @api.post("/mock/config")
    async def update_config(body: ConfigUpdateRequest) -> dict[str, Any]:
        updates = body.model_dump(exclude_unset=True)

        if updates.get("agent_url") and not is_loopback(updates["agent_url"]):
            raise HTTPException(
                status_code=400,
                detail=f"agent_url must resolve to a loopback host (D3): {updates['agent_url']!r}",
            )
        if "agent_timeout" in updates and updates["agent_timeout"] is not None and updates["agent_timeout"] <= 0:
            raise HTTPException(status_code=400, detail="agent_timeout must be > 0")
        if "min_confidence" in updates and updates["min_confidence"] is not None and not (
            0.0 <= updates["min_confidence"] <= 1.0
        ):
            raise HTTPException(status_code=400, detail="min_confidence must be within [0, 1]")
        if "max_pending" in updates and updates["max_pending"] is not None and updates["max_pending"] <= 0:
            raise HTTPException(status_code=400, detail="max_pending must be > 0")

        for field, value in updates.items():
            setattr(application.config, field, value)

        return {"config": application.config.model_dump(mode="json")}

    return api
