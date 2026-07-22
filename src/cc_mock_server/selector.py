"""Endpoint selector (plan.md phase 4).

`EndpointSelector.is_selected(request)` decides, in live mode, whether a
given request should be routed to the agent at all. Two independent axes
of state:

- **Explicit overrides**: patterns explicitly `select()`ed or
  `deselect()`ed, at either domain granularity (`"api.stripe.com"`) or
  method+path granularity (`"GET api.stripe.com/v1/charges"` — the path
  portion is compared via `matcher.normalize_path`, reusing phase 3's
  conservative id normalization so `/v1/charges/ch_123` still matches a
  pattern written against `/v1/charges/ch_abc`).
- **Default state**: `auto_select_filtered` (True by default) resolves
  the live-mode chicken-and-egg problem — with no explicit selection at
  all, every endpoint that already passed the domain `Filter` counts as
  selected. Setting it to False flips the default: nothing is selected
  until the agent/user explicitly opts an endpoint in. `select_all()` /
  `select_none()` reset the default for the current session without
  touching the `auto_select_filtered` config value itself.

Explicit overrides always win over the current default, and method+path
overrides win over domain-level overrides (more specific wins). All
mutation happens under `asyncio.Lock`, never `threading.Lock` (D1/D6).
"""

from __future__ import annotations

import asyncio

from cc_mock_server.filter import host_matches
from cc_mock_server.matcher import normalize_path
from cc_mock_server.models import Request

#: HTTP method tokens that, as the first whitespace-separated token of a
#: pattern, mark it as a method+path pattern rather than a domain pattern.
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})


def _parse_pattern(pattern: str) -> tuple[str, str]:
    """Classify `pattern` and return `(kind, normalized_key)`.

    `kind` is `"method_path"` (first token is an HTTP method) or
    `"domain"`. For `method_path`, the key is
    `"{METHOD} {host}{normalized_path}"`; for `domain` it's the lowercased
    domain pattern (still possibly a wildcard, matched via
    `filter.host_matches` at lookup time rather than baked in here).
    """
    stripped = pattern.strip()
    parts = stripped.split(None, 1)
    if len(parts) == 2 and parts[0].upper() in _HTTP_METHODS:
        method = parts[0].upper()
        rest = parts[1].strip()
        host, sep, path_tail = rest.partition("/")
        path = f"/{path_tail}" if sep else "/"
        key = f"{method} {host.lower()}{normalize_path(path)}"
        return "method_path", key
    return "domain", stripped.lower()


def _request_method_path_key(request: Request) -> str:
    """Build the same-shaped key as `_parse_pattern` for an incoming request."""
    return f"{request.method.upper()} {request.host.lower()}{normalize_path(request.path)}"


class EndpointSelector:
    """Runtime-mutable set of "which endpoints does the agent handle" state."""

    def __init__(
        self,
        auto_select_filtered: bool = True,
        *,
        lock: asyncio.Lock | None = None,
    ) -> None:
        self.auto_select_filtered = auto_select_filtered
        self._lock = lock if lock is not None else asyncio.Lock()
        # Explicit per-pattern overrides: True = selected, False = deselected.
        self._method_path_overrides: dict[str, bool] = {}
        self._domain_overrides: dict[str, bool] = {}
        # Session-wide default set by select_all()/select_none(); None means
        # "fall back to auto_select_filtered".
        self._all_override: bool | None = None

    def is_selected(self, request: Request) -> bool:
        """Return True if `request` should be routed to the agent.

        Precedence: explicit method+path override > explicit domain
        override (wildcard-aware) > session-wide select_all/select_none >
        `auto_select_filtered` default.
        """
        method_path_key = _request_method_path_key(request)
        if method_path_key in self._method_path_overrides:
            return self._method_path_overrides[method_path_key]

        host = request.host.lower()
        for pattern, selected in self._domain_overrides.items():
            if host_matches(pattern, host):
                return selected

        if self._all_override is not None:
            return self._all_override
        return self.auto_select_filtered

    async def select(self, pattern: str) -> None:
        """Explicitly mark `pattern` (domain or method+path) as selected."""
        async with self._lock:
            kind, key = _parse_pattern(pattern)
            if kind == "method_path":
                self._method_path_overrides[key] = True
            else:
                self._domain_overrides[key] = True

    async def deselect(self, pattern: str) -> None:
        """Explicitly mark `pattern` (domain or method+path) as deselected."""
        async with self._lock:
            kind, key = _parse_pattern(pattern)
            if kind == "method_path":
                self._method_path_overrides[key] = False
            else:
                self._domain_overrides[key] = False

    async def select_all(self) -> None:
        """Clear explicit overrides and treat every endpoint as selected."""
        async with self._lock:
            self._method_path_overrides.clear()
            self._domain_overrides.clear()
            self._all_override = True

    async def select_none(self) -> None:
        """Clear explicit overrides and treat every endpoint as deselected."""
        async with self._lock:
            self._method_path_overrides.clear()
            self._domain_overrides.clear()
            self._all_override = False
