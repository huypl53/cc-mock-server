---
phase: 3
title: "Fuzzy Matcher"
status: pending
priority: P1
dependencies: [1, 2]
---

# Phase 3: Fuzzy Matcher

## Overview
Logic thuần: sinh `fuzzy_key` từ request và match incoming request với recordings, bỏ dynamic fields, **và gate theo confidence** (D4) để không trả recording sai âm thầm.

## Requirements
- Functional: `fuzzy_key(request) -> str` = `METHOD::host::path_pattern`. Normalize segment thành `{id}` theo rule **cụ thể** (H3-clarify):
  - toàn số: `^\d+$`
  - UUID: regex uuid v1–5
  - prefixed id kiểu Stripe: `^[a-z]+_[A-Za-z0-9]{6,}$` (vd `ch_abc123`, `cus_abc123`)
  - **KHÔNG** normalize slug thường: `/users/john`, `/repos/my-app` giữ nguyên (negative test bắt buộc)
- Functional: `match(request, recordings) -> MatchResult | None`. Lọc theo fuzzy_key; nhiều candidate → so query keys (sorted, bỏ values) + body structure (key set, chỉ JSON — D8); tie-break `recorded_at` mới nhất.
- Functional: `confidence_score(request, recording) -> float` ∈ [0,1] (path exact vs normalized, query overlap, body-structure overlap).
- Functional (D4): `match()` trả `None` nếu best confidence < `min_confidence` → caller đi fallback. Đây là hành vi test-được, không phải log.
- Non-functional: pure, không I/O.

## Architecture
`matcher.py`: `fuzzy_key()`, `normalize_path()`, `body_structure()` (recursive key set, JSON-only), `confidence_score()`, `match(request, recordings, min_confidence)`. Regex rules là hằng số module, comment rõ.

## Related Code Files
- Create: `src/cc_mock_server/matcher.py`
- Create: `tests/test_matcher.py`

## Implementation Steps (TDD)
1. **RED**:
   - normalize: numeric/uuid/prefixed-id → `{id}`; **negative**: `john`, `my-app`, `v1`, `charges` KHÔNG normalize.
   - fuzzy_key ổn định bất kể query order.
   - `/users/123` và `/users/456` cùng fuzzy_key.
   - match nhiều candidate → structure gần nhất; tie → mới nhất.
   - **confidence gate**: match dưới `min_confidence` trả `None`.
   - non-JSON body → bỏ body tie-break, không crash.
2. **GREEN**: implement tới pass.
3. **REFACTOR**: tách regex constants + scoring weights.

## Success Criteria
- [ ] `pytest tests/test_matcher.py` pass.
- [ ] Positive + **negative** normalization đều có test.
- [ ] Query order bất biến.
- [ ] Low-confidence → `None` (không trả recording sai).
- [ ] Tie-break theo timestamp.

## Risk Assessment
- Over-normalize slug thật. Mitigation: rule bảo thủ + negative tests + confidence gate là lưới an toàn thứ hai.
