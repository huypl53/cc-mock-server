"""Composition root (plan.md phase 6, D1).

Builds every component from a `Config`, wires the H4 fuzzy_key backfill
(`matcher.fuzzy_key` injected into `ModeRouter`, resolving the
recorder<->matcher circular import at the one place both already exist),
and configures a single mitmproxy `Master` that runs `MockAddon`.

Single event-loop wiring (D1): `Application` is only fully buildable from
*inside* a running event loop, because `mitmproxy.master.Master` calls
`asyncio.get_running_loop()` (when no `event_loop` is given) and several of
mitmproxy's default addons (notably `Proxyserver`) schedule tasks during
construction/`configure`. `run_proxy()` then runs that same `Master` via
`asyncio.gather(...)` on the CURRENT loop -- phase 7 appends
`uvicorn.Server.serve()` to that same `gather(...)` call so the control API
and the proxy share one loop, never two. `Application` is the object phase
7 attaches its FastAPI app to: it exposes the exact same `config`,
`domain_filter`, `selector`, `recorder`, `agent_handler` instances the
running proxy uses (single owner of each piece of mutable state, D6) so a
`POST /mock/mode` (etc.) mutates the live pipeline directly, with no second
construction and no cross-loop bridging required for anything except the
documented `AgentHandler.respond(..., loop=...)` fallback (D1).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mitmproxy import options as mitm_options
from mitmproxy.addons import default_addons
from mitmproxy.master import Master

from cc_mock_server.agent_handler import AgentHandler
from cc_mock_server.config import Config
from cc_mock_server.filter import DomainFilter
from cc_mock_server.matcher import fuzzy_key, match
from cc_mock_server.recorder import Recorder
from cc_mock_server.router import ModeRouter
from cc_mock_server.selector import EndpointSelector
from cc_mock_server.server import MockAddon


@dataclass
class Application:
    """Composition root shared with phase 7's control API.

    One instance per running server; every field is the single live owner
    of its piece of state (D6) -- phase 7's FastAPI app must reuse these
    instances directly rather than constructing its own.
    """

    config: Config
    domain_filter: DomainFilter
    selector: EndpointSelector
    recorder: Recorder
    agent_handler: AgentHandler
    router: ModeRouter
    addon: MockAddon
    master: Master


def build_components(
    config: Config,
) -> tuple[DomainFilter, EndpointSelector, Recorder, AgentHandler, ModeRouter]:
    """Build every component that needs no running event loop.

    Split out from `build_application` so tests (and a future CLI
    `--check-config` style command) can validate wiring without spinning up
    mitmproxy at all.
    """
    domain_filter = DomainFilter(mode=config.filter_mode, domains=config.filter_domains)
    selector = EndpointSelector()
    recorder = Recorder(config.recordings_dir)
    recorder.load_all()
    agent_handler = AgentHandler(config)
    router = ModeRouter(
        config,
        domain_filter,
        selector,
        agent_handler,
        recorder,
        match_fn=match,
        fuzzy_key_fn=fuzzy_key,
    )
    return domain_filter, selector, recorder, agent_handler, router


def build_application(
    config: Config,
    *,
    confdir: Optional[Path] = None,
    event_loop: Optional[asyncio.AbstractEventLoop] = None,
) -> Application:
    """Build every component AND the mitmproxy `Master` (D1).

    Must be called from within a running event loop. `confdir` overrides
    `Options.confdir` (default `~/.mitmproxy`) -- pass a temp directory in
    tests so the generated CA (`{confdir}/mitmproxy-ca-cert.pem`) is
    hermetic and disposable per test/process rather than shared global
    mitmproxy state.
    """
    domain_filter, selector, recorder, agent_handler, router = build_components(config)
    addon = MockAddon(router, domain_filter)

    opts = mitm_options.Options(
        listen_host="127.0.0.1",
        listen_port=config.proxy_port,
        mode=["regular"],
    )
    if confdir is not None:
        opts.confdir = str(confdir)

    master = Master(opts, event_loop=event_loop)
    master.addons.add(*default_addons())
    master.addons.add(addon)

    return Application(
        config=config,
        domain_filter=domain_filter,
        selector=selector,
        recorder=recorder,
        agent_handler=agent_handler,
        router=router,
        addon=addon,
        master=master,
    )


async def run_proxy(app: Application) -> None:
    """Run the mitmproxy master on the CURRENT event loop (D1).

    Phase 7 extends this `asyncio.gather(...)` with
    `uvicorn.Server.serve()` so both servers share one loop. If a future
    dependency ever forces the control API onto a separate thread, pending
    `AgentHandler` futures must still only ever be resolved via
    `loop.call_soon_threadsafe(...)` (see `AgentHandler.respond(...,
    loop=...)`) -- never a direct cross-thread `set_result`.
    """
    await asyncio.gather(app.master.run())


async def shutdown(app: Application) -> None:
    """Stop the mitmproxy master and release the agent handler's HTTP
    client. `Master.shutdown()` is documented as thread-safe (it uses
    `call_soon_threadsafe` internally), so this may be invoked from any
    thread; the `aclose()` awaits must run on `app`'s own loop though."""
    app.master.shutdown()
    await app.agent_handler.aclose()
