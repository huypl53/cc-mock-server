"""`cc-mock` CLI (plan.md phase 7).

`build_arg_parser()` is driven entirely by `help.COMMANDS`/`help.GLOBAL_PARAMS`
(the same registry `help.render_agent_help()` reads) -- this file adds no
command of its own that isn't in that registry, which is what keeps
`cc-mock --agent-help` from drifting out of sync with the actual argparse
setup (`tests/test_cli.py`'s drift test asserts this).

`start` builds a real `Application` (`app.build_application`) and a real
`uvicorn.Server`, then runs BOTH on one event loop via a single
`asyncio.gather(...)` (D1) -- exactly the pattern proven end-to-end by
`tests/test_cross_loop.py`. Every other subcommand (`status`, `mode`,
`filter`, `select`, `deselect`, `respond`, `recordings`, `pending`) is a
thin synchronous `httpx` client against that running control API.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx
import uvicorn

from pathlib import Path

from cc_mock_server import help as help_registry
from cc_mock_server import installer
from cc_mock_server.app import build_application, shutdown
from cc_mock_server.config import Config, load_config
from cc_mock_server.control_api import create_control_api
from cc_mock_server.help import ParamSpec

_TYPE_MAP: dict[str, Callable[[str], Any]] = {"str": str, "int": int, "float": float}


def _add_param(parser: argparse.ArgumentParser, param: ParamSpec) -> None:
    """Translate one registry `ParamSpec` into an `add_argument(...)` call.

    Every argparse default is `None` (never `param.default`, which is
    presentation-only text for `--agent-help`): `start` relies on
    `load_config`'s CLI/env/YAML/default precedence to fill unset fields,
    and the client subcommands' `_control_base_url()` applies the two
    connection params' real fallback itself. Keeping a single "unset ==
    None" convention here avoids two different defaulting code paths.
    """
    type_fn = _TYPE_MAP[param.type]
    kwargs: dict[str, Any] = {"help": param.help, "type": type_fn}
    if param.choices:
        kwargs["choices"] = param.choices

    if param.is_positional:
        if param.optional_positional:
            kwargs["nargs"] = "?"
            kwargs["default"] = None
        parser.add_argument(param.name, **kwargs)
        return

    kwargs["dest"] = param.name
    kwargs["default"] = None
    if param.append:
        kwargs["action"] = "append"
    if param.required:
        kwargs["required"] = True
    parser.add_argument(param.flag, **kwargs)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the full `cc-mock` argparse tree from `help.COMMANDS` --
    the single source of truth also used by `help.render_agent_help()`."""
    parser = argparse.ArgumentParser(
        prog="cc-mock", description="Smart mocking proxy for AI coding agents."
    )
    parser.add_argument(
        "--agent-help",
        action="store_true",
        help="Print a structured markdown command reference for an LLM agent, then exit.",
    )

    # Shared by every subcommand (never duplicated per-command): which
    # control API to talk to (client commands) / bind to (`start`).
    connection_parent = argparse.ArgumentParser(add_help=False)
    for param in help_registry.GLOBAL_PARAMS:
        _add_param(connection_parent, param)

    subparsers = parser.add_subparsers(dest="command")
    for command in help_registry.COMMANDS:
        sub = subparsers.add_parser(
            command.name, help=command.summary, parents=[connection_parent]
        )
        for param in command.params:
            _add_param(sub, param)

    return parser


def get_subparser_names(parser: argparse.ArgumentParser) -> set[str]:
    """Introspect `parser` for its registered subcommand names (argparse
    exposes no cleaner public API for this) -- used by the drift test to
    confirm they exactly match `help.command_names()`."""
    for action in parser._actions:  # noqa: SLF001 -- no public equivalent
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices.keys())
    return set()


# ----------------------------------------------------------------------------
# `start`: proxy + control API on one loop (D1)
# ----------------------------------------------------------------------------


def _build_start_config(args: argparse.Namespace) -> Config:
    cli_overrides = {
        "proxy_port": args.proxy_port,
        "control_port": args.control_port,
        "control_bind": args.control_host,
        "mode": args.mode,
        "agent_url": args.agent_url,
        "agent_mode": args.agent_mode,
        "filter_mode": args.filter_mode,
        "filter_domains": args.filter,
        "agent_timeout": args.agent_timeout,
        "timeout_fallback": args.timeout_fallback,
        "min_confidence": args.min_confidence,
        "recordings_dir": args.recordings,
        "stream_delay": args.stream_delay,
    }
    return load_config(cli_overrides=cli_overrides)


async def _run_start(config: Config) -> None:
    application = build_application(config)
    api = create_control_api(application)
    uvicorn_config = uvicorn.Config(
        api, host=config.control_bind, port=config.control_port, log_level="info"
    )
    server = uvicorn.Server(uvicorn_config)
    print(
        f"cc-mock: proxy on 127.0.0.1:{config.proxy_port}, "
        f"control API on {config.control_bind}:{config.control_port}",
        file=sys.stderr,
    )
    # D1: ONE loop, both halves scheduled as tasks on it -- the same idea
    # as appending uvicorn.Server.serve() to the gather app.run_proxy()
    # would otherwise build alone. Plain `asyncio.gather(...)` would wait
    # for BOTH to finish; `uvicorn.Server.serve()` installs its own
    # SIGINT/SIGTERM handler (`capture_signals()`) and returns as soon as
    # it sees one, but that alone would never stop `master.run()` -- the
    # process would hang forever after Ctrl-C. `asyncio.wait(...,
    # FIRST_COMPLETED)` lets either half's exit (signal-triggered, or a
    # crash) drive the other's shutdown via the SAME `shutdown()` this
    # module's tests already exercise (`app.shutdown` stops the mitmproxy
    # master; the `respond`d agent handler's client is closed too).
    master_task = asyncio.create_task(application.master.run())
    server_task = asyncio.create_task(server.serve())
    try:
        done, _pending = await asyncio.wait(
            {master_task, server_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
    finally:
        server.should_exit = True
        await shutdown(application)
        await asyncio.gather(master_task, server_task, return_exceptions=True)


def cmd_start(args: argparse.Namespace) -> int:
    config = _build_start_config(args)
    try:
        asyncio.run(_run_start(config))
    except KeyboardInterrupt:  # pragma: no cover -- interactive-only path
        print("cc-mock: shutting down", file=sys.stderr)
    return 0


# ----------------------------------------------------------------------------
# client subcommands: thin httpx wrappers around the control API
# ----------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Install the cc-mock skill + CLAUDE.md block (local, no control API)."""
    scope = getattr(args, "scope", None) or "global"
    written = installer.install(scope=scope, home=Path.home(), cwd=Path.cwd())
    print(f"cc-mock: installed into Claude Code ({scope} scope):")
    for path in written:
        print(f"  - {path}")
    print("Restart Claude Code (or start a new session) to pick up the skill.")
    return 0


def _control_base_url(args: argparse.Namespace) -> str:
    host = getattr(args, "control_host", None) or "127.0.0.1"
    port = getattr(args, "control_port", None) or 8081
    return f"http://{host}:{port}"


def _print_response(resp: httpx.Response) -> int:
    try:
        payload = resp.json()
    except ValueError:
        payload = resp.text
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, indent=2))
    else:
        print(payload)
    return 0 if resp.is_success else 1


def _request(args: argparse.Namespace, method: str, path: str, **kwargs: Any) -> int:
    base_url = _control_base_url(args)
    try:
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            resp = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        print(f"cc-mock: could not reach control API at {base_url}: {exc}", file=sys.stderr)
        return 1
    return _print_response(resp)


def cmd_status(args: argparse.Namespace) -> int:
    return _request(args, "GET", "/mock/status")


def cmd_mode(args: argparse.Namespace) -> int:
    return _request(args, "POST", "/mock/mode", json={"mode": args.mode})


def cmd_filter(args: argparse.Namespace) -> int:
    if args.action == "list":
        return _request(args, "GET", "/mock/filter")
    if not args.domain:
        print("cc-mock: filter add/remove requires a domain argument", file=sys.stderr)
        return 1
    return _request(args, "POST", "/mock/filter", json={"action": args.action, "domain": args.domain})


def cmd_select(args: argparse.Namespace) -> int:
    return _request(args, "POST", "/mock/select", json={"pattern": args.pattern})


def cmd_deselect(args: argparse.Namespace) -> int:
    return _request(args, "DELETE", f"/mock/select/{quote(args.pattern, safe='/')}")


def cmd_respond(args: argparse.Namespace) -> int:
    try:
        body = json.loads(args.json)
    except json.JSONDecodeError as exc:
        print(f"cc-mock: invalid --json payload: {exc}", file=sys.stderr)
        return 1
    payload = {
        "request_id": args.request_id,
        "status": args.status if args.status is not None else 200,
        "body": body,
    }
    return _request(args, "POST", "/mock/respond", json=payload)


def cmd_recordings(args: argparse.Namespace) -> int:
    if args.delete:
        return _request(args, "DELETE", f"/mock/recordings/{args.delete}")
    return _request(args, "GET", "/mock/recordings")


def cmd_pending(args: argparse.Namespace) -> int:
    return _request(args, "GET", "/mock/pending")


_CLIENT_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "status": cmd_status,
    "mode": cmd_mode,
    "filter": cmd_filter,
    "select": cmd_select,
    "deselect": cmd_deselect,
    "respond": cmd_respond,
    "recordings": cmd_recordings,
    "pending": cmd_pending,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.agent_help:
        print(help_registry.render_agent_help())
        return 0

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "start":
        return cmd_start(args)

    if args.command == "init":
        return cmd_init(args)

    handler = _CLIENT_HANDLERS.get(args.command)
    if handler is None:  # pragma: no cover -- argparse already restricts choices
        parser.print_help()
        return 1
    return handler(args)
