"""`cc-mock init` -- install cc-mock into Claude Code (Tier 1).

Mirrors the `rtk init` / `gitnexus setup` pattern: one command writes every
artifact so the user never hand-copies files. Two artifacts, both idempotent:

- a skill at `<base>/skills/cc-mock/SKILL.md` that teaches Claude the
  poll -> respond mocking loop (invoked when a task involves mocking APIs);
- a managed block in a `CLAUDE.md` so Claude always knows the tool exists
  (the RTK `--claude-md` idea), delimited by markers so re-running replaces
  it in place instead of duplicating.

All functions are pure over explicit paths (no implicit `~` / `os.getcwd`
reads) so tests drive them against `tmp_path`; `cli.cmd_init` resolves the
real `Path.home()` / cwd and calls in.
"""

from __future__ import annotations

from pathlib import Path

START_MARKER = "<!-- cc-mock:start -->"
END_MARKER = "<!-- cc-mock:end -->"

SKILL_MD = """\
---
name: cc-mock
description: >-
  Mock third-party HTTP APIs during development so code that calls external
  services (Stripe, OpenAI, etc.) runs without hitting the real API. Invoke
  when building, testing, or debugging code that makes third-party API calls
  and the real API is unavailable, costly, rate-limited, or returns data
  unsuitable for the case under test.
---

# cc-mock -- mock third-party APIs while coding

`cc-mock` is a local proxy: the app under development routes its HTTP calls
through it (`HTTP_PROXY`), the proxy intercepts calls to filtered domains and
blocks them, and YOU (this agent) compose the response by looking at the app's
code. Answered exchanges are recorded and replayed later with no agent needed.

## The loop (agent_mode=pending, the default)

1. Start it (background), filtering the API domain(s) the app calls:
   `cc-mock start --mode live --agent-mode pending --filter "api.stripe.com" &`
2. Run the app pointed at the proxy: `HTTP_PROXY=http://localhost:8080 <run app>`.
3. The app's API call blocks. Discover it: `cc-mock pending`.
4. Read the app code to learn the response shape it expects, then answer.
   Omit `--request-id` -- cc-mock auto-targets the one blocked request, so
   this is a SINGLE command with no id to copy:
   `cc-mock respond --status 200 --json '<body>'`
   (Streaming/SSE endpoint? answer with `--chunk` instead of `--json`:
   `cc-mock respond --chunk 'data: {"delta":"hi"}' --chunk 'data: [DONE]'`.)
5. Once enough exchanges are recorded, replay them without answering again:
   `cc-mock mode replay` -- re-run the app; matching requests return recordings.

## Notes

- Run each `cc-mock` step as ONE command. Don't capture ids/values into shell
  variables across multiple lines (`ID=$(...)` then use `$ID`) -- omitting
  `--request-id` removes the only reason you'd need to, and keeps every step a
  single inline command that can't be mis-split by a bash-line hook.
- Only pass `--request-id <id>` (from `pending`) when several requests are
  blocked at once; with one blocked request you never need it.
- Only domains in `--filter` are intercepted; everything else passes through to
  the real internet untouched.
- `cc-mock --agent-help` prints the full command reference (params, examples,
  response shapes) -- read it when you need exact flags.
- Sensitive headers (Authorization/Cookie/...) are masked before they reach you
  and before anything is written to disk.
"""

CLAUDE_MD_BODY = """\
## Mocking third-party APIs with cc-mock

When developing or testing code that calls a third-party HTTP API, prefer
`cc-mock` over hitting the real API:

1. `cc-mock start --mode live --agent-mode pending --filter "<api-domain>" &`
2. Run the app with `HTTP_PROXY=http://localhost:8080`.
3. When a request blocks, `cc-mock pending` shows it; read the app code to learn
   the expected response, then answer with a single command (no id needed --
   cc-mock auto-targets the blocked request): `cc-mock respond --json '<body>'`.
   Avoid multi-line bash that assigns an id to a shell variable first.
4. After recording enough exchanges, `cc-mock mode replay` serves them with no
   agent involvement.

Run `cc-mock --agent-help` for the full reference (or invoke the `cc-mock` skill).\
"""


def render_claude_block() -> str:
    """The full managed block (markers included) written into a CLAUDE.md."""
    return f"{START_MARKER}\n{CLAUDE_MD_BODY}\n{END_MARKER}"


def upsert_managed_block(existing: str, block: str) -> str:
    """Return `existing` with the cc-mock managed `block` inserted or replaced.

    Idempotent: if the markers are already present, the region between them
    (inclusive) is replaced; otherwise the block is appended. Content outside
    the markers is never touched.
    """
    start = existing.find(START_MARKER)
    end = existing.find(END_MARKER)
    if start != -1 and end != -1 and end > start:
        end += len(END_MARKER)
        return existing[:start] + block + existing[end:]
    if not existing:
        return block + "\n"
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return existing + sep + block + "\n"


def resolve_targets(scope: str, home: Path, cwd: Path) -> tuple[Path, Path]:
    """Resolve (skill_path, claude_md_path) for the given `scope`.

    - "global":  ~/.claude/skills/cc-mock/SKILL.md  +  ~/.claude/CLAUDE.md
    - "project": <cwd>/.claude/skills/cc-mock/SKILL.md  +  <cwd>/CLAUDE.md
    """
    if scope == "project":
        base = cwd / ".claude"
        return base / "skills" / "cc-mock" / "SKILL.md", cwd / "CLAUDE.md"
    if scope == "global":
        base = home / ".claude"
        return base / "skills" / "cc-mock" / "SKILL.md", base / "CLAUDE.md"
    raise ValueError(f"unknown scope: {scope!r} (expected 'global' or 'project')")


def install(scope: str, home: Path, cwd: Path) -> list[Path]:
    """Write the skill + upsert the CLAUDE.md block. Returns paths written."""
    skill_path, claude_md_path = resolve_targets(scope, home, cwd)

    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(SKILL_MD, encoding="utf-8")

    existing = claude_md_path.read_text(encoding="utf-8") if claude_md_path.exists() else ""
    claude_md_path.parent.mkdir(parents=True, exist_ok=True)
    claude_md_path.write_text(upsert_managed_block(existing, render_claude_block()), encoding="utf-8")

    return [skill_path, claude_md_path]
