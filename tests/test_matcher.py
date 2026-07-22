"""RED-first tests for cc_mock_server.matcher (plan.md phase 3).

Covers every phase-03 success criterion: conservative segment normalization
(positive + REQUIRED negative slug cases), query-order invariance, fuzzy_key
stability, multi-candidate tie-break (structure closeness then newest
recorded_at), the D4 confidence gate (`match()` returns `None` below
`min_confidence`), and non-JSON bodies not crashing / not participating in
the body-structure tie-break (D8).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cc_mock_server.matcher import (
    MatchResult,
    body_structure,
    confidence_score,
    fuzzy_key,
    match,
    normalize_path,
)
from cc_mock_server.models import Recording, RecordingMetadata, Request, Response

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_request(**overrides) -> Request:
    defaults = dict(
        method="GET",
        url="http://api.example.com/v1/users/123",
        host="api.example.com",
        path="/v1/users/123",
        query={},
        headers={},
        body="",
        is_json=False,
        content_type=None,
    )
    defaults.update(overrides)
    return Request(**defaults)


def make_recording(
    *,
    recording_id: str = "rec-1",
    request: Request | None = None,
    recorded_at: datetime = _BASE_TIME,
    status_code: int = 200,
    response_body: str = '{"ok": true}',
) -> Recording:
    return Recording(
        id=recording_id,
        request=request if request is not None else make_request(),
        response=Response(
            status_code=status_code,
            headers={},
            body=response_body,
            is_json=True,
            content_type="application/json",
        ),
        metadata=RecordingMetadata(recorded_at=recorded_at, source="live", fuzzy_key=None),
    )


# --------------------------------------------------------------------------
# normalize_path: positive normalization
# --------------------------------------------------------------------------


class TestNormalizePathPositive:
    def test_numeric_segment_normalized(self):
        assert normalize_path("/users/123") == "/users/{id}"

    def test_uuid_v4_segment_normalized(self):
        assert (
            normalize_path("/orders/550e8400-e29b-41d4-a716-446655440000")
            == "/orders/{id}"
        )

    def test_uuid_case_insensitive_normalized(self):
        assert (
            normalize_path("/orders/550E8400-E29B-41D4-A716-446655440000")
            == "/orders/{id}"
        )

    def test_stripe_style_prefixed_id_normalized(self):
        assert normalize_path("/v1/charges/ch_abc123") == "/v1/charges/{id}"
        assert normalize_path("/v1/customers/cus_ABC123def") == "/v1/customers/{id}"

    def test_multiple_segments_normalized_independently(self):
        assert normalize_path("/users/123/orders/456") == "/users/{id}/orders/{id}"

    def test_numeric_and_uuid_and_prefixed_together(self):
        assert (
            normalize_path("/a/1/b/550e8400-e29b-41d4-a716-446655440000/c/ch_abcdef")
            == "/a/{id}/b/{id}/c/{id}"
        )


# --------------------------------------------------------------------------
# normalize_path: REQUIRED negative cases (must NOT normalize plain slugs)
# --------------------------------------------------------------------------


class TestNormalizePathNegative:
    def test_plain_username_slug_not_normalized(self):
        assert normalize_path("/users/john") == "/users/john"

    def test_hyphenated_slug_not_normalized(self):
        assert normalize_path("/repos/my-app") == "/repos/my-app"

    def test_version_segment_not_normalized(self):
        assert normalize_path("/v1/charges") == "/v1/charges"

    def test_plain_resource_name_not_normalized(self):
        assert normalize_path("/charges") == "/charges"

    def test_short_prefixed_like_token_below_min_length_not_normalized(self):
        # "ch_1" has only 1 char after "_", below the 6-char Stripe-style min.
        assert normalize_path("/v1/charges/ch_1") == "/v1/charges/ch_1"

    def test_root_path_unchanged(self):
        assert normalize_path("/") == "/"

    def test_empty_path_unchanged(self):
        assert normalize_path("") == ""


# --------------------------------------------------------------------------
# fuzzy_key
# --------------------------------------------------------------------------


class TestFuzzyKey:
    def test_format_is_method_host_pattern(self):
        req = make_request(method="get", host="api.example.com", path="/users/123")
        assert fuzzy_key(req) == "GET::api.example.com::/users/{id}"

    def test_stable_regardless_of_query_order(self):
        req_a = make_request(query={"a": "1", "b": "2"})
        req_b = make_request(query={"b": "2", "a": "1"})
        assert fuzzy_key(req_a) == fuzzy_key(req_b)

    def test_different_numeric_ids_share_fuzzy_key(self):
        req_123 = make_request(path="/users/123", url="http://api.example.com/users/123")
        req_456 = make_request(path="/users/456", url="http://api.example.com/users/456")
        assert fuzzy_key(req_123) == fuzzy_key(req_456)

    def test_different_slugs_do_not_share_fuzzy_key(self):
        req_john = make_request(path="/users/john")
        req_jane = make_request(path="/users/jane")
        assert fuzzy_key(req_john) != fuzzy_key(req_jane)


# --------------------------------------------------------------------------
# body_structure: JSON-only recursive key set (D8)
# --------------------------------------------------------------------------


class TestBodyStructure:
    def test_flat_json_object_keys(self):
        req = make_request(
            body='{"name": "x", "age": 1}', is_json=True, content_type="application/json"
        )
        assert body_structure(req) == frozenset({"name", "age"})

    def test_nested_json_keys_collected_recursively(self):
        req = make_request(
            body='{"user": {"name": "x", "address": {"city": "y"}}}',
            is_json=True,
            content_type="application/json",
        )
        assert body_structure(req) == frozenset({"user", "name", "address", "city"})

    def test_json_array_of_objects_keys_collected(self):
        req = make_request(
            body='[{"id": 1}, {"name": "x"}]', is_json=True, content_type="application/json"
        )
        assert body_structure(req) == frozenset({"id", "name"})

    def test_non_json_body_returns_none(self):
        req = make_request(body="not json at all", is_json=False, content_type="text/plain")
        assert body_structure(req) is None

    def test_empty_body_returns_none(self):
        req = make_request(body="", is_json=False)
        assert body_structure(req) is None

    def test_json_scalar_body_returns_empty_set(self):
        req = make_request(body="42", is_json=True, content_type="application/json")
        assert body_structure(req) == frozenset()


# --------------------------------------------------------------------------
# confidence_score
# --------------------------------------------------------------------------


class TestConfidenceScore:
    def test_identical_request_scores_high(self):
        req = make_request(query={"a": "1"}, body='{"x": 1}', is_json=True)
        recording = make_recording(request=req)
        score = confidence_score(req, recording)
        assert 0.0 <= score <= 1.0
        assert score > 0.9

    def test_exact_path_scores_higher_than_normalized_only(self):
        recorded = make_request(path="/users/123")
        exact = make_request(path="/users/123")
        normalized_only = make_request(path="/users/999")
        recording = make_recording(request=recorded)

        assert confidence_score(exact, recording) >= confidence_score(
            normalized_only, recording
        )

    def test_query_overlap_increases_score(self):
        recorded = make_request(query={"a": "1", "b": "2"})
        recording = make_recording(request=recorded)

        matching = make_request(query={"a": "1", "b": "2"})
        disjoint = make_request(query={"c": "3", "d": "4"})

        assert confidence_score(matching, recording) > confidence_score(disjoint, recording)

    def test_score_bounded_in_unit_interval(self):
        req = make_request(query={"a": "1"}, body='{"x": 1}', is_json=True)
        recording = make_recording(request=make_request(query={"z": "9"}, body="{}", is_json=True))
        score = confidence_score(req, recording)
        assert 0.0 <= score <= 1.0

    def test_non_json_body_does_not_crash_and_stays_bounded(self):
        req = make_request(body="binary-ish", is_json=False, content_type="application/octet-stream")
        recording = make_recording(
            request=make_request(body="other-binary", is_json=False, content_type="application/octet-stream")
        )
        score = confidence_score(req, recording)
        assert 0.0 <= score <= 1.0


# --------------------------------------------------------------------------
# match(): filtering, multi-candidate tie-break, confidence gate (D4)
# --------------------------------------------------------------------------


class TestMatch:
    def test_no_candidates_returns_none(self):
        req = make_request(path="/users/123")
        recording = make_recording(request=make_request(path="/orders/1"))
        assert match(req, [recording], min_confidence=0.0) is None

    def test_single_candidate_above_threshold_returned(self):
        req = make_request(path="/users/123")
        recording = make_recording(request=make_request(path="/users/999"))
        result = match(req, [recording], min_confidence=0.0)
        assert isinstance(result, MatchResult)
        assert result.recording is recording

    def test_multi_candidate_picks_closer_body_structure(self):
        incoming = make_request(
            method="POST",
            path="/v1/charges",
            body='{"amount": 100, "currency": "usd"}',
            is_json=True,
            content_type="application/json",
        )
        close = make_recording(
            recording_id="close",
            request=make_request(
                method="POST",
                path="/v1/charges",
                body='{"amount": 200, "currency": "eur"}',
                is_json=True,
                content_type="application/json",
            ),
            recorded_at=_BASE_TIME,
        )
        far = make_recording(
            recording_id="far",
            request=make_request(
                method="POST",
                path="/v1/charges",
                body='{"unrelated_field": true}',
                is_json=True,
                content_type="application/json",
            ),
            recorded_at=_BASE_TIME,
        )
        result = match(incoming, [far, close], min_confidence=0.0)
        assert result is not None
        assert result.recording.id == "close"

    def test_multi_candidate_tie_break_by_newest_recorded_at(self):
        incoming = make_request(path="/users/123")
        older = make_recording(
            recording_id="older",
            request=make_request(path="/users/111"),
            recorded_at=_BASE_TIME,
        )
        newer = make_recording(
            recording_id="newer",
            request=make_request(path="/users/222"),
            recorded_at=_BASE_TIME + timedelta(hours=1),
        )
        result = match(incoming, [older, newer], min_confidence=0.0)
        assert result is not None
        assert result.recording.id == "newer"

    def test_confidence_gate_returns_none_below_threshold(self):
        incoming = make_request(
            method="POST",
            path="/v1/charges",
            query={"a": "1", "b": "2", "c": "3"},
            body='{"amount": 100}',
            is_json=True,
            content_type="application/json",
        )
        weak = make_recording(
            request=make_request(
                method="POST",
                path="/v1/charges",
                query={"x": "9", "y": "9", "z": "9"},
                body='{"totally_different_shape": 1}',
                is_json=True,
                content_type="application/json",
            )
        )
        # Force an unreachable bar: even a decent match cannot satisfy this.
        assert match(incoming, [weak], min_confidence=1.01) is None

    def test_confidence_gate_passes_above_threshold(self):
        incoming = make_request(path="/users/123")
        recording = make_recording(request=make_request(path="/users/123"))
        result = match(incoming, [recording], min_confidence=0.5)
        assert result is not None

    def test_non_json_body_candidate_does_not_crash_match(self):
        incoming = make_request(
            method="POST",
            path="/upload",
            body="binary-data",
            is_json=False,
            content_type="application/octet-stream",
        )
        recording = make_recording(
            request=make_request(
                method="POST",
                path="/upload",
                body="other-binary-data",
                is_json=False,
                content_type="application/octet-stream",
            )
        )
        result = match(incoming, [recording], min_confidence=0.0)
        assert result is not None
        assert result.recording is recording

    def test_empty_recordings_returns_none(self):
        assert match(make_request(), [], min_confidence=0.0) is None
