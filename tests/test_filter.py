"""RED-first tests for cc_mock_server.filter (plan.md phase 4).

Covers every phase-04 filter success criterion: whitelist/blacklist
semantics, `*.example.com` wildcard matching (subdomain only, not the bare
apex domain), and runtime add/remove mutation under `asyncio.Lock` (D6 —
never `threading.Lock`).
"""

from __future__ import annotations

import asyncio

import pytest

from cc_mock_server.enums import FilterMode
from cc_mock_server.filter import DomainFilter, host_matches


# --------------------------------------------------------------------------
# host_matches: shared wildcard helper
# --------------------------------------------------------------------------


class TestHostMatches:
    def test_exact_match(self):
        assert host_matches("example.com", "example.com") is True

    def test_exact_mismatch(self):
        assert host_matches("example.com", "other.com") is False

    def test_wildcard_matches_subdomain(self):
        assert host_matches("*.example.com", "api.example.com") is True

    def test_wildcard_does_not_match_bare_apex_domain(self):
        assert host_matches("*.example.com", "example.com") is False

    def test_case_insensitive(self):
        assert host_matches("*.Example.com", "API.EXAMPLE.COM") is True


# --------------------------------------------------------------------------
# DomainFilter.should_intercept: whitelist / blacklist / wildcard
# --------------------------------------------------------------------------


class TestDomainFilterWhitelist:
    def test_listed_domain_intercepted(self):
        f = DomainFilter(mode=FilterMode.WHITELIST, domains=["api.stripe.com"])
        assert f.should_intercept("api.stripe.com") is True

    def test_unlisted_domain_not_intercepted(self):
        f = DomainFilter(mode=FilterMode.WHITELIST, domains=["api.stripe.com"])
        assert f.should_intercept("api.github.com") is False

    def test_wildcard_subdomain_intercepted(self):
        f = DomainFilter(mode=FilterMode.WHITELIST, domains=["*.example.com"])
        assert f.should_intercept("api.example.com") is True

    def test_wildcard_does_not_intercept_apex(self):
        f = DomainFilter(mode=FilterMode.WHITELIST, domains=["*.example.com"])
        assert f.should_intercept("example.com") is False


class TestDomainFilterBlacklist:
    def test_listed_domain_not_intercepted(self):
        f = DomainFilter(mode=FilterMode.BLACKLIST, domains=["ads.example.com"])
        assert f.should_intercept("ads.example.com") is False

    def test_unlisted_domain_intercepted(self):
        f = DomainFilter(mode=FilterMode.BLACKLIST, domains=["ads.example.com"])
        assert f.should_intercept("api.stripe.com") is True

    def test_wildcard_blacklist_excludes_subdomains_only(self):
        f = DomainFilter(mode=FilterMode.BLACKLIST, domains=["*.internal.example.com"])
        assert f.should_intercept("svc.internal.example.com") is False
        assert f.should_intercept("internal.example.com") is True


# --------------------------------------------------------------------------
# Runtime mutation under asyncio.Lock (D6)
# --------------------------------------------------------------------------


class TestDomainFilterRuntimeMutation:
    async def test_add_domain_changes_result(self):
        f = DomainFilter(mode=FilterMode.WHITELIST, domains=[])
        assert f.should_intercept("api.stripe.com") is False
        await f.add_domain("api.stripe.com")
        assert f.should_intercept("api.stripe.com") is True

    async def test_remove_domain_changes_result(self):
        f = DomainFilter(mode=FilterMode.WHITELIST, domains=["api.stripe.com"])
        assert f.should_intercept("api.stripe.com") is True
        await f.remove_domain("api.stripe.com")
        assert f.should_intercept("api.stripe.com") is False

    async def test_remove_unknown_domain_is_a_noop(self):
        f = DomainFilter(mode=FilterMode.WHITELIST, domains=["api.stripe.com"])
        await f.remove_domain("not-there.com")
        assert f.should_intercept("api.stripe.com") is True

    def test_uses_asyncio_lock_not_threading_lock(self):
        f = DomainFilter(mode=FilterMode.WHITELIST, domains=[])
        assert isinstance(f._lock, asyncio.Lock)

    async def test_concurrent_add_and_remove_no_error(self):
        f = DomainFilter(mode=FilterMode.WHITELIST, domains=[])

        async def add_many():
            for i in range(20):
                await f.add_domain(f"host{i}.com")

        async def remove_many():
            for i in range(20):
                await f.remove_domain(f"host{i}.com")

        await asyncio.gather(add_many(), remove_many())
        # No assertion on final membership (race-dependent); the point is
        # that concurrent mutation under the lock doesn't raise.
