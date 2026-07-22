"""Pure helpers for recorder path-safety (H3) and header masking (D3).

Kept dependency-free (no `models`/`config` imports) so both `recorder.py`
and its tests can exercise them in isolation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Mapping

#: Header names masked on write, case-insensitively (D3). Config-driven in
#: the sense that `Recorder(mask_headers=...)` may override this default.
DEFAULT_SENSITIVE_HEADERS = frozenset(
    {"authorization", "api-key", "x-api-key", "cookie", "set-cookie"}
)

#: Value written in place of a masked header.
MASK_VALUE = "***"

#: Anything outside this set is collapsed to "_" when building filesystem
#: path components — keeps filenames portable across OSes and prevents
#: separator/traversal characters from surviving sanitization.
_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")

_MAX_FILENAME_BYTES = 255
_EXTENSION = ".json"


def mask_headers(
    headers: Mapping[str, str], sensitive: Iterable[str] = DEFAULT_SENSITIVE_HEADERS
) -> dict[str, str]:
    """Return a copy of `headers` with sensitive keys replaced by `MASK_VALUE`.

    Matching is case-insensitive on the header name; the original mapping
    is never mutated.
    """
    sensitive_lower = {name.lower() for name in sensitive}
    return {
        key: (MASK_VALUE if key.lower() in sensitive_lower else value)
        for key, value in headers.items()
    }


def sanitize_host(host: str) -> str:
    """Neutralize a `Host` header value for use as a single directory name.

    The `Host` header is attacker-controlled and may contain path
    separators or `..` sequences (H3). Separators are replaced first so
    the result can never contain `/` or `\\` (i.e. it is always exactly
    one path component, never a multi-segment traversal). Remaining
    unsafe characters collapse to `_`; a value that degenerates to only
    dots/underscores (e.g. `".."`) falls back to a safe default instead
    of resolving to the current or parent directory.
    """
    cleaned = host.strip().lower().replace("/", "_").replace("\\", "_")
    cleaned = _UNSAFE_CHARS_RE.sub("_", cleaned)
    cleaned = cleaned.strip("._")
    return cleaned or "unknown-host"


def slugify_path(path: str) -> str:
    """Convert a URL path into a filesystem-safe slug.

    Any query string accidentally included is stripped, and leading /
    trailing slashes are removed before unsafe characters collapse to
    `_`. An empty/root path yields `"root"`.
    """
    bare = path.split("?", 1)[0].strip("/")
    if not bare:
        return "root"
    slug = _UNSAFE_CHARS_RE.sub("_", bare).strip("_")
    return slug or "root"


def build_filename(
    method: str,
    path_slug: str,
    timestamp: str,
    suffix: str,
    max_bytes: int = _MAX_FILENAME_BYTES,
) -> str:
    """Compose `{METHOD}_{path_slug}_{timestamp}_{suffix}.json`, capped at
    `max_bytes` (H3). The `_{timestamp}_{suffix}.json` tail is never
    truncated — it carries the uniqueness guarantee (D5) — only the
    `{METHOD}_{path_slug}` head is trimmed, on a UTF-8 character boundary,
    when the full name would exceed the cap.
    """
    method_norm = _UNSAFE_CHARS_RE.sub("_", method.strip().upper()) or "METHOD"
    tail = f"_{timestamp}_{suffix}{_EXTENSION}"
    head = f"{method_norm}_{path_slug}"

    budget = max_bytes - len(tail.encode("utf-8"))
    if budget <= 0:
        # Pathological max_bytes too small even for the tail alone; still
        # return a parseable (if minimal) name rather than raising.
        return tail.lstrip("_")

    head_bytes = head.encode("utf-8")[:budget]
    # Never split a multi-byte UTF-8 codepoint in half.
    while head_bytes and (head_bytes[-1] & 0b1100_0000) == 0b1000_0000:
        head_bytes = head_bytes[:-1]
    head_safe = head_bytes.decode("utf-8", errors="ignore")
    return f"{head_safe}{tail}"


def confine_path(base_dir: Path, *parts: str) -> Path:
    """Resolve `base_dir.joinpath(*parts)` and confine it under `base_dir`.

    Raises `ValueError` if the resolved path escapes `base_dir` (H3). This
    is defense-in-depth on top of `sanitize_host`/`slugify_path`, which
    already strip path separators — this check catches any future caller
    that passes unsanitized input directly.
    """
    base_resolved = base_dir.resolve()
    candidate = base_resolved.joinpath(*parts).resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes recordings_dir: {candidate}") from exc
    return candidate
