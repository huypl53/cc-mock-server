"""RED-first tests for cc_mock_server.selector (plan.md phase 4).

Covers every phase-04 selector success criterion: domain-level and
method+path-level pattern grammar (path compared via
`matcher.normalize_path`), select/deselect/select_all/select_none under
lock, and BOTH directions of the `auto_select_filtered` chicken-egg
resolution (True = selected-by-default-unless-deselected, False =
must-explicitly-select).
"""

from __future__ import annotations

import asyncio

import pytest

from cc_mock_server.models import Request
from cc_mock_server.selector import EndpointSelector


def make_request(**overrides) -> Request:
    defaults = dict(
        method="GET",
        url="https://api.stripe.com/v1/charges",
        host="api.stripe.com",
        path="/v1/charges",
        query={},
        headers={},
        body="",
        is_json=False,
        content_type=None,
    )
    defaults.update(overrides)
    return Request(**defaults)


# --------------------------------------------------------------------------
# auto_select_filtered=True (default): selected unless explicitly deselected
# --------------------------------------------------------------------------


class TestAutoSelectFilteredDefaultTrue:
    def test_unselected_endpoint_is_selected_by_default(self):
        selector = EndpointSelector()
        assert selector.is_selected(make_request()) is True

    async def test_explicit_deselect_domain_turns_off(self):
        selector = EndpointSelector()
        await selector.deselect("api.stripe.com")
        assert selector.is_selected(make_request()) is False

    async def test_explicit_deselect_method_path_turns_off_only_that_endpoint(self):
        selector = EndpointSelector()
        await selector.deselect("GET api.stripe.com/v1/charges")
        assert selector.is_selected(make_request(method="GET", path="/v1/charges")) is False
        # A different endpoint on the same host stays selected (auto default).
        assert selector.is_selected(make_request(method="GET", path="/v1/customers")) is True

    async def test_reselect_after_deselect_turns_back_on(self):
        selector = EndpointSelector()
        await selector.deselect("api.stripe.com")
        await selector.select("api.stripe.com")
        assert selector.is_selected(make_request()) is True


# --------------------------------------------------------------------------
# auto_select_filtered=False: must explicitly select
# --------------------------------------------------------------------------


class TestAutoSelectFilteredFalse:
    def test_unselected_endpoint_is_not_selected(self):
        selector = EndpointSelector(auto_select_filtered=False)
        assert selector.is_selected(make_request()) is False

    async def test_explicit_domain_select_turns_on(self):
        selector = EndpointSelector(auto_select_filtered=False)
        await selector.select("api.stripe.com")
        assert selector.is_selected(make_request()) is True

    async def test_explicit_method_path_select_turns_on_only_that_endpoint(self):
        selector = EndpointSelector(auto_select_filtered=False)
        await selector.select("GET api.stripe.com/v1/charges")
        assert selector.is_selected(make_request(method="GET", path="/v1/charges")) is True
        assert selector.is_selected(make_request(method="GET", path="/v1/customers")) is False

    async def test_deselect_after_select_turns_back_off(self):
        selector = EndpointSelector(auto_select_filtered=False)
        await selector.select("api.stripe.com")
        await selector.deselect("api.stripe.com")
        assert selector.is_selected(make_request()) is False


# --------------------------------------------------------------------------
# Method+path grammar: path compared via matcher.normalize_path
# --------------------------------------------------------------------------


class TestMethodPathPatternNormalization:
    async def test_method_path_pattern_matches_normalized_id_segment(self):
        selector = EndpointSelector(auto_select_filtered=False)
        await selector.select("GET api.stripe.com/v1/charges/ch_abc123")
        # Same normalized shape, different concrete id -> still matches.
        assert (
            selector.is_selected(make_request(method="GET", path="/v1/charges/ch_xyz999"))
            is True
        )

    async def test_method_token_is_case_insensitive(self):
        selector = EndpointSelector(auto_select_filtered=False)
        await selector.select("get api.stripe.com/v1/charges")
        assert selector.is_selected(make_request(method="GET", path="/v1/charges")) is True

    async def test_wrong_method_does_not_match(self):
        selector = EndpointSelector(auto_select_filtered=False)
        await selector.select("GET api.stripe.com/v1/charges")
        assert selector.is_selected(make_request(method="POST", path="/v1/charges")) is False


# --------------------------------------------------------------------------
# select_all / select_none
# --------------------------------------------------------------------------


class TestSelectAllNone:
    async def test_select_all_selects_everything(self):
        selector = EndpointSelector(auto_select_filtered=False)
        await selector.select_all()
        assert selector.is_selected(make_request()) is True
        assert selector.is_selected(make_request(host="other.example.com")) is True

    async def test_select_none_deselects_everything(self):
        selector = EndpointSelector(auto_select_filtered=True)
        await selector.select_none()
        assert selector.is_selected(make_request()) is False

    async def test_select_all_then_explicit_deselect_overrides_one_endpoint(self):
        selector = EndpointSelector(auto_select_filtered=False)
        await selector.select_all()
        await selector.deselect("api.stripe.com")
        assert selector.is_selected(make_request()) is False
        assert selector.is_selected(make_request(host="other.example.com")) is True


# --------------------------------------------------------------------------
# Lock type (D1/D6): asyncio.Lock, never threading.Lock
# --------------------------------------------------------------------------


class TestSelectorLock:
    def test_uses_asyncio_lock(self):
        selector = EndpointSelector()
        assert isinstance(selector._lock, asyncio.Lock)

    async def test_concurrent_select_deselect_no_error(self):
        selector = EndpointSelector()

        async def selects():
            for i in range(20):
                await selector.select(f"host{i}.com")

        async def deselects():
            for i in range(20):
                await selector.deselect(f"host{i}.com")

        await asyncio.gather(selects(), deselects())
