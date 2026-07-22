"""Pure SSE (Server-Sent Events) helpers for streaming capture/replay
(plan.md phase 8, D10).

These three functions are the entire "SSE domain logic" for cc-mock-server
and are deliberately kept free of any mitmproxy/asyncio dependency so they
can be unit-tested in isolation (`tests/test_streaming.py`):

- `is_sse`: is this response a Server-Sent Events stream? (detection)
- `parse_sse_events`: split a raw SSE body into its individual events.
- `frame_sse_events`: the inverse -- re-join events into a wire-correct body.

Everything else (the actual pass-through tee + capture, and replay) lives in
`server.py`/`router.py`, which call into this module rather than
duplicating SSE framing logic.
"""

from __future__ import annotations

from typing import Iterable, Mapping


def is_sse(headers: Mapping[str, str]) -> bool:
    """Return True if `headers` describes a `text/event-stream` response.

    Looks up `Content-Type` case-insensitively (mitmproxy's own header
    object is already case-insensitive, but callers may hand this a plain
    `dict` -- e.g. `models.Response.headers` -- so this must not assume any
    particular key casing). A `; charset=...`-style suffix is ignored, same
    as `models.is_text_content_type`.
    """
    for key, value in headers.items():
        if key.lower() == "content-type":
            bare = value.split(";", 1)[0].strip().lower()
            return bare == "text/event-stream"
    return False


def parse_sse_events(body: str) -> list[str]:
    """Split a raw SSE body into its individual events.

    SSE events are separated by a blank line (`\\n\\n`); this normalizes
    `\\r\\n` line endings first so both Unix- and wire-style (CRLF) bodies
    split the same way. A trailing blank-line artifact (from a body that
    ends with `\\n\\n`) is dropped rather than yielded as an empty event.
    The `data: [DONE]` sentinel some LLM APIs (OpenAI) emit as their final
    event is ordinary text to this function -- it is kept verbatim as its
    own list entry, exactly like every other event.
    """
    if not body:
        return []
    normalized = body.replace("\r\n", "\n")
    return [event for event in normalized.split("\n\n") if event.strip()]


def frame_sse_events(events: Iterable[str]) -> bytes:
    """Re-join `events` (as produced by `parse_sse_events`) into a
    wire-correct SSE body: each event terminated by a blank line, encoded
    as UTF-8. Inverse of `parse_sse_events` (round-trips: `parse_sse_events
    (frame_sse_events(events).decode()) == list(events)`, given non-empty
    events)."""
    events = list(events)
    if not events:
        return b""
    return ("\n\n".join(events) + "\n\n").encode("utf-8")
