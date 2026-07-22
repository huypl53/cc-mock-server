"""Full model surface for cc-mock-server (plan.md D9).

Defined entirely up-front so later phases (3 fuzzy matcher, 5 agent
handler, 6 proxy/router) can import stable types without churn:

- `Request` / `Response`: wire-level HTTP data, content-type aware (D8).
- `RecordingMetadata` / `Recording`: what `Recorder` (phase 2) persists.
- `HandlerResult`: what an agent handler (phase 5) returns.
- `PendingRequest`: an in-flight `agent_mode=pending` request (phase 5/6).

Body encoding (D8): a body whose content-type is text-safe (`text/*`,
`application/json`, xml, form-urlencoded, javascript) is stored as decoded
UTF-8 text; anything else (images, octet-stream, multipart, gzip, ...) or
text that fails UTF-8 decoding is stored as base64. `is_json` is only ever
`True` when the text body is valid JSON. `encode_body`/`decode_body` are
the single source of truth for this decision so callers never have to
duplicate the classification logic.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

#: Structured (non `text/*`) content-types that are still safe to store as
#: plain UTF-8 text rather than base64.
_TEXT_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/x-www-form-urlencoded",
        "application/javascript",
        "application/ld+json",
    }
)


def is_text_content_type(content_type: Optional[str]) -> bool:
    """Return True if `content_type` denotes a text-safe body (D8).

    A missing/empty content-type is treated as text (empty or unknown
    bodies are small and rarely binary). `text/*` and a fixed set of
    structured text types are text; everything else (images,
    octet-stream, multipart, gzip, ...) must be base64-encoded.
    """
    if not content_type:
        return True
    bare = content_type.split(";", 1)[0].strip().lower()
    return bare in _TEXT_CONTENT_TYPES or bare.startswith("text/")


def _looks_like_json(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    try:
        json.loads(stripped)
    except (ValueError, TypeError):
        return False
    return True


def encode_body(raw_body: bytes, content_type: Optional[str]) -> tuple[str, bool]:
    """Encode `raw_body` for storage on a `Request`/`Response` model.

    Returns `(body, is_json)`. Text-safe content-types are decoded as
    UTF-8; anything else (or text that fails to decode) is base64-encoded
    with `is_json=False`.
    """
    if not raw_body:
        return "", False
    if is_text_content_type(content_type):
        try:
            text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(raw_body).decode("ascii"), False
        return text, _looks_like_json(text)
    return base64.b64encode(raw_body).decode("ascii"), False


def decode_body(body: str, content_type: Optional[str]) -> bytes:
    """Invert `encode_body`: return the original raw bytes for `body`."""
    if not body:
        return b""
    if is_text_content_type(content_type):
        return body.encode("utf-8")
    return base64.b64decode(body)


class Request(BaseModel):
    """An intercepted HTTP request (recorded, replayed, or sent to an agent).

    `query` and `headers` collapse repeated keys to their last value —
    an accepted simplification for a mocking proxy (documented, not a bug).
    """

    method: str
    url: str
    host: str
    path: str
    query: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    is_json: bool = False
    content_type: Optional[str] = None

    @classmethod
    def from_raw(
        cls,
        *,
        method: str,
        url: str,
        host: str,
        path: str,
        query: Optional[dict[str, str]] = None,
        headers: Optional[dict[str, str]] = None,
        raw_body: bytes = b"",
        content_type: Optional[str] = None,
    ) -> "Request":
        """Build a `Request`, encoding `raw_body` per D8 content-type rules."""
        body, is_json = encode_body(raw_body, content_type)
        return cls(
            method=method,
            url=url,
            host=host,
            path=path,
            query=query or {},
            headers=headers or {},
            body=body,
            is_json=is_json,
            content_type=content_type,
        )

    def decoded_body(self) -> bytes:
        """Return the original raw bytes for `body` (inverse of `from_raw`)."""
        return decode_body(self.body, self.content_type)


class Response(BaseModel):
    """An HTTP response — from an agent, a recording, or a built-in handler."""

    status_code: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    is_json: bool = False
    content_type: Optional[str] = None

    @classmethod
    def from_raw(
        cls,
        *,
        status_code: int,
        headers: Optional[dict[str, str]] = None,
        raw_body: bytes = b"",
        content_type: Optional[str] = None,
    ) -> "Response":
        """Build a `Response`, encoding `raw_body` per D8 content-type rules."""
        body, is_json = encode_body(raw_body, content_type)
        return cls(
            status_code=status_code,
            headers=headers or {},
            body=body,
            is_json=is_json,
            content_type=content_type,
        )

    def decoded_body(self) -> bytes:
        """Return the original raw bytes for `body` (inverse of `from_raw`)."""
        return decode_body(self.body, self.content_type)


class RecordingMetadata(BaseModel):
    """Bookkeeping attached to a `Recording`.

    `fuzzy_key` is intentionally nullable (H4): computing it would create
    a circular import between `recorder.py` (phase 2) and `matcher.py`
    (phase 3). `Recorder.save` always writes `fuzzy_key=None`; the
    composition root (phase 6) injects `matcher.fuzzy_key` as a callable
    to backfill it after both components exist.
    """

    recorded_at: datetime
    source: str
    fuzzy_key: Optional[str] = None


class Recording(BaseModel):
    """A persisted request/response pair. `id` is the filename stem (H4):
    stable and NOT content-derived, so history survives re-recording the
    same request.
    """

    id: str
    request: Request
    response: Response
    metadata: RecordingMetadata


class HandlerResult(BaseModel):
    """What an agent handler (phase 5) returns for a live-mode request."""

    action: Literal["respond", "pass_through"]
    response: Optional[Response] = None

    @model_validator(mode="after")
    def _respond_requires_a_response(self) -> "HandlerResult":
        if self.action == "respond" and self.response is None:
            raise ValueError("HandlerResult(action='respond') requires a response")
        return self


@dataclass
class PendingRequest:
    """An in-flight `agent_mode=pending` request awaiting `POST /mock/respond`.

    Not a pydantic model: `future` is an `asyncio.Future`, which is a
    purely in-memory synchronization primitive that is never serialized
    and doesn't have meaningful validation/equality semantics.
    """

    request_id: str
    request: Request
    future: "asyncio.Future[Response]"
    created_at: datetime
