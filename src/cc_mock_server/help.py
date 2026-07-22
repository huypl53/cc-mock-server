"""Single-source command registry for `cc-mock` (plan.md phase 7).

`COMMANDS` (plus `GLOBAL_PARAMS`) is the ONE place every CLI command's
shape is described: its params, an example invocation, its response shape,
and the error codes an agent should expect. `cli.py`'s argparse wiring and
`render_agent_help()` below are both DERIVED from this registry -- neither
hand-maintains its own copy of the command list, which is exactly what
keeps `cc-mock --agent-help` from drifting out of sync with what the CLI
actually accepts (enforced by `tests/test_cli.py`'s drift test).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ParamSpec:
    """One argparse argument, described data-only (no callables) so this
    same object can drive both `add_argument(...)` and markdown rendering.

    `flag` is `None` for a positional argument (`name` is used as-is);
    otherwise `flag` is the `--long-form` argparse option string and `name`
    is the destination.
    """

    name: str
    help: str
    flag: Optional[str] = None
    type: str = "str"  # "str" | "int" | "float"
    required: bool = False
    default: Optional[str] = None
    choices: tuple[str, ...] = ()
    append: bool = False  # argparse action="append" (repeatable flag)
    optional_positional: bool = False  # nargs="?" for a positional

    @property
    def is_positional(self) -> bool:
        return self.flag is None

    def display(self) -> str:
        """Render this param's shape for `--agent-help`, e.g.
        `--request-id <str> (required)` or `pattern <str>`."""
        choices_suffix = f" choices={{{','.join(self.choices)}}}" if self.choices else ""
        if self.is_positional:
            marker = "" if self.required or not self.optional_positional else " (optional)"
            return f"{self.name} <{self.type}>{choices_suffix}{marker}"
        required_suffix = " (required)" if self.required else f" (default: {self.default})"
        append_suffix = " [repeatable]" if self.append else ""
        return f"{self.flag} <{self.type}>{choices_suffix}{append_suffix}{required_suffix}"


@dataclass(frozen=True)
class CommandSpec:
    """One `cc-mock <name>` subcommand, fully described for both argparse
    and `--agent-help`."""

    name: str
    summary: str
    params: tuple[ParamSpec, ...]
    example: str
    response_shape: str
    error_codes: tuple[str, ...] = ()
    workflow_note: Optional[str] = None


#: Connection options shared by every subcommand (added via an argparse
#: parent parser in `cli.py`, never duplicated per-command) -- listed here
#: once so `render_agent_help()` documents them exactly once too.
GLOBAL_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec(
        name="control_host",
        flag="--control-host",
        help="Control API host to connect to (also the bind host for `start`).",
        default="127.0.0.1",
    ),
    ParamSpec(
        name="control_port",
        flag="--control-port",
        help="Control API port (also the bind port for `start`).",
        type="int",
        default="8081",
    ),
)


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        name="start",
        summary=(
            "Launch the proxy AND the control API together in one process/loop "
            "(D1). Blocks until interrupted (Ctrl-C)."
        ),
        params=(
            ParamSpec(
                name="proxy_port",
                flag="--proxy-port",
                type="int",
                default="8080",
                help="Proxy listen port -- point HTTP_PROXY at this.",
            ),
            ParamSpec(
                name="mode",
                flag="--mode",
                choices=("live", "replay"),
                default="live",
                help="Initial mode: serve live traffic via the agent, or replay recordings.",
            ),
            ParamSpec(
                name="agent_url",
                flag="--agent-url",
                help=(
                    "Agent callback URL (loopback only, D3), e.g. http://127.0.0.1:9000/cb. "
                    "In sync mode it must return the response ENVELOPE {status, body, ...} -- "
                    "see the 'sync-mode callback contract' section of --agent-help."
                ),
            ),
            ParamSpec(
                name="agent_mode",
                flag="--agent-mode",
                choices=("sync", "pending"),
                default="pending",
                help="Agent transport: pending (poll+respond, default) or sync (inline callback).",
            ),
            ParamSpec(
                name="filter_mode",
                flag="--filter-mode",
                choices=("whitelist", "blacklist"),
                default="whitelist",
                help="Domain filter semantics (D7).",
            ),
            ParamSpec(
                name="filter",
                flag="--filter",
                append=True,
                help="Domain (or *.wildcard) to add to the filter list. Repeatable.",
            ),
            ParamSpec(
                name="agent_timeout",
                flag="--agent-timeout",
                type="float",
                default="10.0",
                help="Seconds to wait for the agent before applying timeout_fallback.",
            ),
            ParamSpec(
                name="timeout_fallback",
                flag="--timeout-fallback",
                choices=("return_error", "pass_through", "built_in"),
                default="return_error",
                help="What to do when the agent times out.",
            ),
            ParamSpec(
                name="min_confidence",
                flag="--min-confidence",
                type="float",
                default="0.6",
                help="Fuzzy-match confidence gate in [0, 1] (D4); below this, replay treats it as a miss.",
            ),
            ParamSpec(
                name="recordings",
                flag="--recordings",
                default="recordings",
                help="Directory recordings are read from / written to.",
            ),
        ),
        example=(
            "cc-mock start --proxy-port 8080 --control-port 8081 --mode live "
            "--agent-mode pending --filter-mode whitelist --filter '*.stripe.com'"
        ),
        response_shape=(
            "None -- this blocks the process running the proxy + control API until "
            "Ctrl-C/SIGTERM. Use another terminal (or the other subcommands below) "
            "to interact with the running instance."
        ),
    ),
    CommandSpec(
        name="init",
        summary=(
            "Install cc-mock into Claude Code: write the `cc-mock` skill and a "
            "managed CLAUDE.md block (idempotent). Runs locally -- no control API."
        ),
        params=(
            ParamSpec(
                name="scope",
                choices=("global", "project"),
                optional_positional=True,
                default="global",
                help="global -> ~/.claude (default); project -> ./.claude + ./CLAUDE.md.",
            ),
        ),
        example="cc-mock init  |  cc-mock init project",
        response_shape="Prints the paths written (SKILL.md + CLAUDE.md).",
        workflow_note=(
            "Run once per machine (global) or per repo (project); re-running "
            "replaces the managed block in place rather than duplicating it."
        ),
    ),
    CommandSpec(
        name="status",
        summary="Print the current mode, agent config, filter, and pending/recordings counts.",
        params=(),
        example="cc-mock status",
        response_shape=(
            "200 JSON: {mode, agent_mode, agent_url, agent_timeout, timeout_fallback, "
            "replay_miss_strategy, min_confidence, max_pending, proxy_port, control_port, "
            "pending_count, recordings_count, filter_mode, filter_domains}"
        ),
        error_codes=("200 OK",),
    ),
    CommandSpec(
        name="mode",
        summary="Switch between live (agent-backed) and replay (recording-backed) mode.",
        params=(
            ParamSpec(name="mode", choices=("live", "replay"), required=True, help="The mode to switch to."),
        ),
        example="cc-mock mode replay",
        response_shape='200 JSON: {"mode": "replay"}',
        error_codes=("422 invalid mode value",),
    ),
    CommandSpec(
        name="filter",
        summary="List, add to, or remove from the domain filter (D7).",
        params=(
            ParamSpec(
                name="action", choices=("add", "remove", "list"), required=True, help="Which filter operation to run."
            ),
            ParamSpec(
                name="domain",
                optional_positional=True,
                help="Domain (or *.wildcard) to add/remove. Required for add/remove, ignored for list.",
            ),
        ),
        example="cc-mock filter add '*.stripe.com'  |  cc-mock filter list",
        response_shape='200 JSON: {"mode": "whitelist", "domains": ["*.stripe.com", ...]}',
        error_codes=("client-side error if `domain` is missing for add/remove",),
    ),
    CommandSpec(
        name="select",
        summary="Explicitly route a domain or `METHOD host/path` pattern to the agent in live mode.",
        params=(
            ParamSpec(name="pattern", required=True, help="A domain (e.g. api.stripe.com) or 'METHOD host/path'."),
        ),
        example="cc-mock select 'POST api.stripe.com/v1/charges'",
        response_shape='200 JSON: {"selected": "POST api.stripe.com/v1/charges"}',
    ),
    CommandSpec(
        name="deselect",
        summary="Explicitly stop routing a domain or `METHOD host/path` pattern to the agent.",
        params=(
            ParamSpec(name="pattern", required=True, help="Same pattern shape as `select`."),
        ),
        example="cc-mock deselect 'POST api.stripe.com/v1/charges'",
        response_shape='200 JSON: {"deselected": "POST api.stripe.com/v1/charges"}',
    ),
    CommandSpec(
        name="respond",
        summary="Resolve a pending `agent_mode=pending` request discovered via `pending` (D1/D2).",
        params=(
            ParamSpec(name="request_id", flag="--request-id", required=True, help="The `request_id` from `pending`."),
            ParamSpec(name="status", flag="--status", type="int", default="200", help="HTTP status code to send back."),
            ParamSpec(
                name="json",
                flag="--json",
                required=True,
                help='JSON response body, e.g. \'{"result": "ok"}\'.',
            ),
        ),
        example='cc-mock respond --request-id 3fa2c1 --status 200 --json \'{"result": "ok"}\'',
        response_shape='200 JSON: {"request_id": "3fa2c1", "resolved": true}',
        error_codes=("404 unknown or already-resolved request_id",),
        workflow_note=(
            "Workflow: poll `pending` to discover in-flight requests, then call "
            "`respond` with the same `request_id` for each one you want to answer."
        ),
    ),
    CommandSpec(
        name="recordings",
        summary="List all recordings, or delete one by id (id = filename stem).",
        params=(
            ParamSpec(
                name="delete", flag="--delete", help="Recording id to delete. Omit to list all recordings instead."
            ),
        ),
        example="cc-mock recordings  |  cc-mock recordings --delete POST_v1_charges_20260101T000000000000_ab12cd34",
        response_shape=(
            '200 JSON (list): {"recordings": [...]}  |  200 JSON (delete): {"deleted": "<id>"}'
        ),
        error_codes=("404 unknown recording id (delete only)",),
    ),
    CommandSpec(
        name="pending",
        summary="List requests currently blocked waiting for `respond` (agent_mode=pending).",
        params=(),
        example="cc-mock pending",
        response_shape=(
            '200 JSON: {"pending": [{"request_id", "request": {method, url, host, path, '
            'headers, body, ...}, "created_at"}]}'
        ),
        workflow_note=(
            "Workflow: poll this endpoint, then call `respond` with each entry's "
            "`request_id` once you've composed a response for it."
        ),
    ),
)


def command_names() -> tuple[str, ...]:
    """The single source of truth for "what subcommands exist" -- both
    `cli.build_arg_parser()` and this module's own `render_agent_help()`
    iterate `COMMANDS` directly; this helper just exposes the name list for
    the drift test."""
    return tuple(command.name for command in COMMANDS)


def _render_params(params: tuple[ParamSpec, ...]) -> str:
    if not params:
        return "  (no parameters)"
    return "\n".join(f"  - `{param.display()}` -- {param.help}" for param in params)


def render_agent_help() -> str:
    """Structured markdown for `cc-mock --agent-help`: purpose, params,
    example, response shape, and error codes for every command, generated
    entirely from `COMMANDS`/`GLOBAL_PARAMS` (no hand-maintained duplicate)."""
    lines: list[str] = [
        "# cc-mock -- agent reference",
        "",
        "Control API base URL: `http://{control-host}:{control-port}/mock/*` "
        "(defaults `127.0.0.1:8081`).",
        "",
        "## Global options (every subcommand)",
        "",
        _render_params(GLOBAL_PARAMS),
        "",
    ]
    for command in COMMANDS:
        lines.append(f"## `{command.name}`")
        lines.append("")
        lines.append(command.summary)
        lines.append("")
        lines.append("Params:")
        lines.append(_render_params(command.params))
        lines.append("")
        lines.append(f"Example: `{command.example}`")
        lines.append("")
        lines.append(f"Response: {command.response_shape}")
        if command.error_codes:
            lines.append("")
            lines.append("Error codes: " + "; ".join(command.error_codes))
        if command.workflow_note:
            lines.append("")
            lines.append(command.workflow_note)
        lines.append("")

    lines.append("## Poll -> respond workflow (agent_mode=pending, the default)")
    lines.append("")
    lines.append(
        "1. `cc-mock pending` to see requests currently blocked waiting for an answer.\n"
        "2. Compose a JSON response for one of them.\n"
        "3. `cc-mock respond --request-id <id> --status <code> --json '<body>'` to unblock it.\n"
        "4. The resolved exchange is recorded automatically for later `replay`."
    )
    lines.append("")
    lines.append("## sync-mode callback contract (agent_mode=sync)")
    lines.append("")
    lines.append(
        "When you run with `--agent-mode sync --agent-url <url>`, the proxy POSTs the "
        "intercepted request to `<url>` and uses that HTTP call's JSON reply as the "
        "response INLINE (no `pending`/`respond` round-trip)."
    )
    lines.append("")
    lines.append(
        "Your callback MUST return a response ENVELOPE, not the raw payload:\n"
        "  `{\"status\": <int, optional, default 200>, \"body\": <json>, "
        "\"headers\": <object, optional>, \"content_type\": <str, optional>}`\n"
        "(`status_code` is accepted as an alias for `status`.)"
    )
    lines.append("")
    lines.append(
        "PITFALL: the real response payload goes under the `body` key. If you return "
        "your payload at the top level (no `body` key), `body` defaults to `{}` and the "
        "app silently receives an empty `{}` -- your data is dropped. Request details "
        "the proxy sends you: `{request_id, method, url, headers, body, is_json, "
        "content_type}` (sensitive headers already masked)."
    )
    lines.append("")
    lines.append(
        "Example callback reply: `{\"status\": 201, \"body\": {\"id\": \"ch_123\"}}` -> "
        "the app receives HTTP 201 with body `{\"id\": \"ch_123\"}`."
    )
    return "\n".join(lines)
