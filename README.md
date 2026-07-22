# cc-mock-server

Smart mocking proxy for AI coding agents. Route app traffic through
`HTTP_PROXY` → cc-mock-server intercepts third-party API calls → an AI
agent (external callback or pending/respond) or built-in handler composes
a JSON response → the exchange is recorded → later replayed via fuzzy
matching.

## Requirements

- Python **3.12+** (see "Python version" note below).

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

This installs the `cc-mock` console script (see `[project.scripts]` in
`pyproject.toml`).

For system-wide use, install the CLI with pipx:

```bash
pipx install git+https://github.com/huypl53/cc-mock-server.git
```

## Use with Claude Code

One command installs cc-mock into Claude Code (writes the `cc-mock` skill and a
managed `CLAUDE.md` block, so the agent knows the poll→respond mocking loop):

```bash
cc-mock init            # global: ~/.claude/skills/cc-mock + ~/.claude/CLAUDE.md
cc-mock init project    # this repo: ./.claude/skills/cc-mock + ./CLAUDE.md
```

Re-running is idempotent (the managed block is replaced in place, never
duplicated). Restart Claude Code afterwards to pick up the skill. In
`agent_mode=pending`, the Claude agent developing the app IS the agent that
composes responses — it discovers blocked requests with `cc-mock pending` and
answers with `cc-mock respond`.

## Quick start

```bash
# 1. Start the proxy + control API in one process (binds 127.0.0.1 for
#    both by default -- the control API never listens on anything else,
#    D3).
cc-mock start \
  --proxy-port 8080 --control-port 8081 \
  --mode live --agent-mode pending \
  --filter-mode whitelist --filter '*.stripe.com' --filter '*.openai.com'

# 2. Point your app at the proxy.
export HTTP_PROXY=http://127.0.0.1:8080
export HTTPS_PROXY=http://127.0.0.1:8080

# 3. (agent_mode=pending, the default) poll for requests waiting on you,
#    then answer them:
cc-mock pending --control-port 8081
cc-mock respond --request-id <id-from-pending> --status 200 \
  --json '{"result": "ok"}' --control-port 8081

# 4. Once you have enough recordings, replay them without an agent:
cc-mock mode replay --control-port 8081
```

Every subcommand below (everything except `start`) is a thin HTTP client
against the control API and takes `--control-host`/`--control-port` to
point at a non-default instance (defaults `127.0.0.1:8081`).

| Command | Purpose |
|---|---|
| `cc-mock start` | Launch the proxy + control API together (blocks). |
| `cc-mock status` | Current mode/agent/filter/pending/recordings counts. |
| `cc-mock mode <live\|replay>` | Switch modes at runtime. |
| `cc-mock filter <add\|remove\|list> [domain]` | Manage the domain filter (D7). |
| `cc-mock select <pattern>` | Route a domain or `METHOD host/path` to the agent in live mode. |
| `cc-mock deselect <pattern>` | Stop routing a pattern to the agent (falls back to replay/pass-through). |
| `cc-mock pending` | List requests currently blocked waiting for `respond`. |
| `cc-mock respond --request-id <id> --status <code> --json '<body>'` | Unblock a pending request. |
| `cc-mock recordings [--delete <id>]` | List or delete recordings. |
| `cc-mock --agent-help` | Full structured markdown reference (params, examples, response shapes, error codes) generated from the same command registry `cc-mock --help` uses -- meant to be pasted into an LLM agent's context. |

Run `cc-mock --agent-help` for the exhaustive, always-in-sync reference
(including every flag `start` accepts: `--proxy-port`, `--control-port`,
`--mode`, `--agent-url`, `--agent-mode`, `--filter-mode`/`--filter`,
`--agent-timeout`, `--timeout-fallback`, `--min-confidence`,
`--recordings`).

## Agent transports: `pending` vs `sync`

- **`pending`** (default): the app request blocks; you discover it with
  `cc-mock pending`, then answer with `cc-mock respond` (the CLI wraps your
  `--status`/`--json` into the response for you). This is the path an LLM
  agent drives.
- **`sync`** (`--agent-mode sync --agent-url <loopback-url>`): the proxy POSTs
  the intercepted request to your callback and uses its JSON reply inline.

**Sync callback contract.** Your callback MUST return a response *envelope*,
not the raw payload:

```json
{"status": 201, "body": {"id": "ch_123"}, "headers": {}, "content_type": "application/json"}
```

Only `body` is required (`status` defaults to `200`; `status_code` is an alias
for `status`). The proxy POSTs you `{request_id, method, url, headers, body,
is_json, content_type}` with sensitive headers already masked.

> **Pitfall:** the real payload goes under the `body` key. If you return your
> payload at the top level (no `body` key), `body` defaults to `{}` and the app
> silently receives an empty `{}` — your data is dropped.

## HTTPS: trusting the mitmproxy CA

HTTPS interception (D7) only ever TLS-terminates domains that are actually
in scope for your configured filter -- everything else is tunneled through
raw, undecrypted, with no certificate substitution at all. For domains you
DO want to intercept over HTTPS, your client must trust the CA certificate
`cc-mock start` (mitmproxy under the hood) generates on first run:

```bash
# Default location (mitmproxy's own confdir):
~/.mitmproxy/mitmproxy-ca-cert.pem
```

1. Start `cc-mock start` at least once so the CA cert gets generated.
2. Trust it for your HTTP client / OS:
   - **curl / httpx / requests**: pass `--cacert ~/.mitmproxy/mitmproxy-ca-cert.pem`
     (curl) or `verify="~/.mitmproxy/mitmproxy-ca-cert.pem"` (httpx/requests).
   - **Node.js**: `NODE_EXTRA_CA_CERTS=~/.mitmproxy/mitmproxy-ca-cert.pem`.
   - **macOS system trust** (affects every app that uses the system store):
     `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ~/.mitmproxy/mitmproxy-ca-cert.pem`
   - **Linux system trust**: copy the `.pem` into
     `/usr/local/share/ca-certificates/`, rename it `.crt`, then run
     `sudo update-ca-certificates`.
3. Domains outside your `--filter`/`--filter-mode` scope never need any of
   this -- they are never TLS-terminated in the first place, so
   cert-pinned clients talking to out-of-scope hosts keep working
   unmodified.

## Out of scope (v1)

- **WebSocket / streaming / SSE mocking.** Many third-party AI APIs (e.g.
  OpenAI, Anthropic) stream responses by default; mitmproxy buffers the
  full body before this proxy can inspect it, which means a streamed
  response is held **fully in memory** before `cc-mock-server` ever sees
  it. If you're mocking a streaming endpoint, either disable streaming on
  the client (`stream: false`) or watch payload size closely -- there is
  no chunked/incremental mocking support in v1.
- Auth / multi-tenant support for the control API (it binds to loopback
  only by default -- see `control_bind`/`--control-host` above; `POST
  /mock/config` also refuses to set a non-loopback `agent_url`, D3).
- A web dashboard UI.
- Automatic recording cleanup/rotation (recordings accumulate on disk;
  monitor disk usage yourself for now, or `cc-mock recordings --delete
  <id>` individually).

## Python version

`mitmproxy` (the underlying proxy engine) requires Python `>=3.12` with
no known upper bound at the time of writing; it is tested upstream against
3.12, 3.13, and 3.14. This project pins `requires-python = ">=3.12"` in
`pyproject.toml` accordingly. If a future `mitmproxy` release drops
support for the interpreter you have installed, cap your virtualenv to
the latest version documented as supported at
https://github.com/mitmproxy/mitmproxy (currently 3.12–3.14) and re-create
`.venv` with that interpreter.
