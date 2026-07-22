---
phase: 4
title: "Filter & Selector"
status: pending
priority: P1
dependencies: [1]
---

# Phase 4: Filter & Selector

## Overview
Hai lớp quyết định: **Filter** (user config: domain nào bị intercept — dùng ở CONNECT stage, D7) và **Selector** (agent runtime: endpoint nào agent handle). Thuần logic + state in-memory có lock (D6).

## Requirements
- Functional Filter: `should_intercept(host) -> bool`. whitelist → chỉ domain trong list; blacklist → tất cả trừ list. Wildcard `*.example.com` (fnmatch). Mutable runtime (add/remove) dưới `asyncio.Lock` (D6).
- Functional Selector: `is_selected(request) -> bool`. Pattern: `"api.stripe.com"` (domain) hoặc `"GET api.stripe.com/v1/charges"` (method+path, path so bằng `matcher.normalize_path`). `select/deselect/select_all/select_none`. State runtime dưới lock.
- **Selector bootstrap (D-clarify chicken-egg)**: live mode nếu chưa select endpoint nào → request không tới agent. Giải: config option `auto_select_filtered: bool = True` — mặc định coi mọi domain đã qua filter là selected trừ khi user/agent deselect. Document sequence trong help (phase 7).
- Non-functional: mutation an toàn trên single loop (`asyncio.Lock`), không dùng threading.Lock (D1/D6).

## Architecture
`filter.py`: `DomainFilter(mode, domains, lock)` — wildcard qua `fnmatch`. `selector.py`: `EndpointSelector(auto_select_filtered, lock)` giữ set patterns; parse grammar rõ ràng: có method token (GET/POST/...) đầu chuỗi → method+path pattern; else domain pattern. Chung helper host-match nếu trùng.

## Related Code Files
- Create: `src/cc_mock_server/filter.py`, `src/cc_mock_server/selector.py`
- Create: `tests/test_filter.py`, `tests/test_selector.py`

## Implementation Steps (TDD)
1. **RED filter**: whitelist pass đúng domain; blacklist đảo; wildcard subdomain; add/remove runtime đổi kết quả.
2. **RED selector**: domain-level select; method+path select (path fuzzy); deselect; select_all/none; `auto_select_filtered=True` → endpoint chưa deselect coi là selected; `=False` → phải explicit select.
3. **GREEN**: implement.
4. **REFACTOR**: chung host-match helper.

## Success Criteria
- [ ] `pytest tests/test_filter.py tests/test_selector.py` pass.
- [ ] Wildcard `*.example.com` match `api.example.com`, không match `example.com`.
- [ ] select/deselect đổi hành vi runtime.
- [ ] `auto_select_filtered` cả hai chiều có test (giải chicken-egg).

## Risk Assessment
- Grammar nhập nhằng domain vs method+path. Mitigation: parse theo method token; test cả hai dạng.
