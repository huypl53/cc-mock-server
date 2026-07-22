---
phase: 6
title: "Proxy & Mode Router"
status: pending
priority: P1
dependencies: [2, 3, 4, 5]
---

# Phase 6: Proxy & Mode Router

## Overview
Ráp mọi thứ: mitmproxy addon bắt request qua `HTTP_PROXY`, filter ở CONNECT stage (D7), chạy pipeline priority với state snapshot + lock (D6), single event-loop wiring (D1). Composition root `app.py` dùng chung cho control API (phase 7).

## Requirements
### Event-loop wiring (D1 — load-bearing)
- `app.py` build tất cả components từ config; chạy `asyncio.gather(master.run(), uvicorn_server.serve())` trên CÙNG loop. Pending Future tạo bằng `get_running_loop()`. Nếu buộc thread-bridge → `call_soon_threadsafe` (documented fallback).

### Filter ở CONNECT stage (D7)
- Addon hook `tls_clienthello`/`http_connect`: `filter.should_intercept(host)` == False → `flow.ignore` / không TLS-terminate (pass-through thật, không cần trust CA, không vỡ cert-pinning).

### Router pipeline (D6)
- `async route(request, on_disconnect) -> HandlerResult`:
  1. snapshot `mode`/filter/selector tại entry (flow đã vào live không đổi giữa chừng)
  2. mode replay → `matcher.match` (confidence-gated); hit→recording; miss→`replay_miss_strategy` (pass_through|live|return_error)
  3. mode live + `selector.is_selected` → `agent_handler.handle`; else theo replay/pass_through
  4. agent timeout → `timeout_fallback`; client disconnect → `HandlerResult` không-record
  5. live thành công (client vẫn connected) → `recorder.save` (masked). fuzzy_key backfill bằng `matcher.fuzzy_key` inject ở đây (giải circular H4).
- Recordings in-memory: `match()` iterate trên snapshot/copy; `save` qua Recorder lock (D6).

### Proxy addon
- `MockAddon`: convert mitmproxy flow ↔ `Request`/`Response`. `pass_through` = KHÔNG set `flow.response`. Đăng ký disconnect callback (mitmproxy `error`/`client_disconnected` hook) → cancel pending.
- Content-type binary → models base64 (D8).

## Architecture
`router.py`: `ModeRouter(config, filter, selector, agent_handler, recorder, matcher)`. `server.py`: `MockAddon`. `app.py`: composition root (build + wire + run gather). Live: load recordings lúc start replay; append qua Recorder owner.

## Related Code Files
- Create: `src/cc_mock_server/router.py`, `src/cc_mock_server/server.py`, `src/cc_mock_server/app.py`
- Create: `tests/test_router.py`, `tests/test_proxy_integration.py`

## Implementation Steps (TDD)
1. **RED router (unit, mock deps)**: verify từng nhánh — CONNECT filter pass-through; replay hit; replay miss→strategy; live selected→handler+record; timeout fallback; disconnect→no record; state snapshot (mode switch giữa flow không đổi hành vi flow đó); concurrent append during match không lỗi.
2. **RED integration** (readiness-poll trước khi fire, deterministic teardown master mỗi test):
   - proxy thật trên port ngẫu nhiên, `httpx proxies=` → hit endpoint → response từ built-in/agent; record file xuất hiện; switch replay → cùng request trả recording không gọi agent.
   - **HTTPS (D7, Global AC #11)**: client `verify=<mitmproxy_ca.pem>` → intercept→record→replay over TLS.
   - domain ngoài filter → pass-through, KHÔNG record, KHÔNG decrypt.
3. **GREEN**: implement addon + router + app.py.
4. **REFACTOR**: tách flow-conversion helper.

## Success Criteria
- [ ] `pytest tests/test_router.py tests/test_proxy_integration.py` pass.
- [ ] E2E HTTP + **HTTPS** intercept→record→replay.
- [ ] Domain ngoài filter pass-through, không record, không decrypt.
- [ ] State snapshot: mode switch mid-flow không ảnh hưởng flow đang chạy.
- [ ] Concurrent save/match không RuntimeError.

## Risk Assessment
- mitmproxy async + event loop conflict. Mitigation: `asyncio.gather(master.run(), server.serve())` (D1); integration khởi động qua `app.py` thật.
- Integration flaky (startup race, master teardown leak, wall-clock timeout). Mitigation: readiness polling, per-test teardown, injected clock cho timeout.
