"""Domain filter (plan.md phase 4, D7).

`DomainFilter.should_intercept(host)` decides, at the CONNECT/
`tls_clienthello` stage, whether a domain is in scope for interception at
all. Domains outside scope must never be TLS-terminated (D7) — that
decision is made by the proxy addon (phase 6) calling this filter; this
module only holds the pure decision logic + runtime-mutable domain list.

Runtime mutation (`add_domain`/`remove_domain`) happens under
`asyncio.Lock`, never `threading.Lock` (D1/D6): the whole app runs on a
single event loop, so the lock only needs to serialize awaits within that
loop, not real OS threads.
"""

from __future__ import annotations

import asyncio
from fnmatch import fnmatch
from typing import Iterable

from cc_mock_server.enums import FilterMode


def host_matches(pattern: str, host: str) -> bool:
    """Return True if `host` matches `pattern` (case-insensitive fnmatch).

    Supports exact domains (`example.com`) and subdomain wildcards
    (`*.example.com`, which matches `api.example.com` but deliberately
    NOT the bare apex `example.com` — fnmatch's `*` can match zero chars,
    but the literal `.` right before `example.com` still has to be
    present in the matched string).
    """
    return fnmatch(host.lower(), pattern.lower())


class DomainFilter:
    """Whitelist/blacklist domain gate with runtime-mutable domain list."""

    def __init__(
        self,
        mode: FilterMode = FilterMode.WHITELIST,
        domains: Iterable[str] = (),
        *,
        lock: asyncio.Lock | None = None,
    ) -> None:
        self.mode = mode
        self._domains: set[str] = {domain.lower() for domain in domains}
        self._lock = lock if lock is not None else asyncio.Lock()

    def should_intercept(self, host: str) -> bool:
        """Return True if `host` should be intercepted under the current mode.

        Whitelist: only domains matching an entry in `domains`.
        Blacklist: every domain EXCEPT those matching an entry in `domains`.
        """
        matched = any(host_matches(pattern, host) for pattern in self._domains)
        if self.mode == FilterMode.WHITELIST:
            return matched
        return not matched

    async def add_domain(self, domain: str) -> None:
        """Add `domain` to the runtime domain list (D6: under `asyncio.Lock`)."""
        async with self._lock:
            self._domains.add(domain.lower())

    async def remove_domain(self, domain: str) -> None:
        """Remove `domain` from the runtime domain list, if present.

        Idempotent no-op if `domain` isn't currently listed.
        """
        async with self._lock:
            self._domains.discard(domain.lower())

    def list_domains(self) -> list[str]:
        """Return the currently configured domains (mutable-safe copy)."""
        return sorted(self._domains)
