"""Fuzzy request matcher (plan.md phase 3).

Pure logic, no I/O: `fuzzy_key()` groups requests by shape (method + host +
normalized path), `match()` picks the best recording for an incoming
request among candidates sharing that shape, and gates the result behind
`min_confidence` (D4) so a low-confidence guess never replays silently as
if it were correct.

Segment normalization is intentionally conservative (H3-clarify): only
all-numeric segments, UUIDs (v1-5), and Stripe-style `prefix_XXXXXX` ids
collapse to `{id}`. Plain slugs (`john`, `my-app`, `v1`, `charges`) are
left untouched — see the negative tests in `tests/test_matcher.py`. The
confidence gate is a second safety net on top of that conservative rule
(see Risk Assessment in phase-03-matcher.md).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from cc_mock_server.models import Recording, Request

#: All-digit path segment, e.g. "123".
_NUMERIC_ID_RE = re.compile(r"^\d+$")

#: UUID versions 1-5, case-insensitive (RFC 4122 layout).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

#: Stripe-style prefixed id, e.g. "ch_abc123", "cus_ABC123def". Requires a
#: lowercase-letters prefix, an underscore, and >=6 alnum chars — chosen to
#: be long enough that it won't accidentally match short real words.
_PREFIXED_ID_RE = re.compile(r"^[a-z]+_[A-Za-z0-9]{6,}$")

#: Scoring weights for `confidence_score`. Path is weighted highest because
#: candidates are already pre-filtered to share a `fuzzy_key` (same
#: normalized path shape) — this component only distinguishes exact vs.
#: normalized-only path matches within that group.
_WEIGHT_PATH = 0.5
_WEIGHT_QUERY = 0.25
_WEIGHT_BODY = 0.25


def _looks_like_id(segment: str) -> bool:
    """Return True if `segment` matches one of the conservative id rules."""
    return bool(
        _NUMERIC_ID_RE.match(segment)
        or _UUID_RE.match(segment)
        or _PREFIXED_ID_RE.match(segment)
    )


def normalize_path(path: str) -> str:
    """Replace id-like path segments with `{id}`, leaving slugs untouched.

    Splitting on "/" preserves leading/trailing slashes (empty segments
    never match an id rule, so they round-trip unchanged).
    """
    segments = path.split("/")
    normalized = ["{id}" if _looks_like_id(segment) else segment for segment in segments]
    return "/".join(normalized)


def fuzzy_key(request: Request) -> str:
    """Return `METHOD::host::normalized_path` — the grouping key for `match()`."""
    return f"{request.method.upper()}::{request.host}::{normalize_path(request.path)}"


def _collect_keys(node: Any) -> set[str]:
    """Recursively collect every object key found anywhere in `node`."""
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            keys |= _collect_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _collect_keys(item)
    return keys


def body_structure(request: Request) -> Optional[frozenset[str]]:
    """Return the recursive set of JSON object keys in `request.body`.

    JSON-only (D8): returns `None` for non-JSON/empty bodies so callers can
    skip the body tie-break instead of crashing on binary/text payloads.
    """
    if not request.is_json or not request.body:
        return None
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return None
    return frozenset(_collect_keys(data))


def _jaccard(a: frozenset[str] | set[str], b: frozenset[str] | set[str]) -> float:
    """Jaccard similarity of two key sets; two empty sets are a perfect match."""
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def confidence_score(request: Request, recording: Recording) -> float:
    """Score how well `recording` fits `request`, in [0, 1].

    Weighted average of three components (each already bounded [0, 1]):
    - path: 1.0 if the literal path matches exactly, 0.85 if it only
      matches after normalization (candidates share a `fuzzy_key`, so this
      is the only remaining path-based signal).
    - query: Jaccard similarity of query-string keys (values ignored, per
      phase-03 spec).
    - body: Jaccard similarity of recursive JSON key sets (D8); omitted
      entirely (not just zeroed) when either side isn't JSON, so a
      non-JSON candidate isn't penalized for a dimension that doesn't
      apply to it.
    """
    recorded_request = recording.request

    components: list[tuple[float, float]] = []

    path_score = 1.0 if request.path == recorded_request.path else 0.85
    components.append((_WEIGHT_PATH, path_score))

    query_score = _jaccard(set(request.query), set(recorded_request.query))
    components.append((_WEIGHT_QUERY, query_score))

    request_structure = body_structure(request)
    recorded_structure = body_structure(recorded_request)
    if request_structure is not None and recorded_structure is not None:
        components.append((_WEIGHT_BODY, _jaccard(request_structure, recorded_structure)))

    total_weight = sum(weight for weight, _ in components)
    if total_weight == 0:
        return 0.0
    return sum(weight * score for weight, score in components) / total_weight


@dataclass(frozen=True)
class MatchResult:
    """The winning recording for a `match()` call, with its confidence."""

    recording: Recording
    confidence: float


def match(
    request: Request,
    recordings: Iterable[Recording],
    min_confidence: float,
) -> Optional[MatchResult]:
    """Find the best recording for `request`, gated by `min_confidence` (D4).

    Filters `recordings` to those sharing `request`'s `fuzzy_key`, scores
    each remaining candidate with `confidence_score`, and picks the
    highest score — ties broken by the newest `metadata.recorded_at`.
    Returns `None` when there are no candidates, or the best score is
    below `min_confidence` (so callers fall back instead of replaying a
    low-confidence guess silently).
    """
    target_key = fuzzy_key(request)
    candidates = [
        recording for recording in recordings if fuzzy_key(recording.request) == target_key
    ]
    if not candidates:
        return None

    scored = [(confidence_score(request, recording), recording) for recording in candidates]
    scored.sort(key=lambda pair: (pair[0], pair[1].metadata.recorded_at), reverse=True)
    best_score, best_recording = scored[0]

    if best_score < min_confidence:
        return None
    return MatchResult(recording=best_recording, confidence=best_score)
