---
phase: 5
title: "Agent Handler"
status: pending
priority: P1
dependencies: [1, 2]
---

# Phase 5: Agent Handler

## Overview
Sinh response cho request đã intercept qua **một trong hai** transport rạch ròi (D2): sync callback XOR pending/respond. Built-in fallback. Xử lý timeout, disconnect, secret masking. Đây là nơi "AI agent tự compose response".

## Requirements
### Transport (D2 — chọn per config `agent_mode`, KHÔNG trộn)
- **`pending` (default, primary cho LLM)**: `handle` tạo `asyncio.Future` (qua `get_running_loop().create_future()` — D1), đăng ký vào `pending: dict[str, PendingRequest]`, expose qua `GET /mock/pending` (phase 7). Nếu có `agent_url` → POST fire-and-forget/202 để notify (không đọc body làm response). Resolution CHỈ qua `respond()`. `await asyncio.wait_for(future, agent_timeout)`.
- **`sync`**: `await httpx.AsyncClient.post(agent_url, json=details)` → dùng JSON body trả về làm response. KHÔNG tạo Future.

### Resolution safety (D1/D2)
- `respond(request_id, status, body)` set future result. Guard `if not fut.done()` (tránh double `set_result` → `InvalidStateError`).
- Cross-thread fallback: nếu control API chạy thread riêng, `respond` phải dùng `master_loop.call_soon_threadsafe`. Single-loop (default) thì set trực tiếp. Interface `respond` nhận loop ref để chọn đúng path.

### Secret masking tới agent (D3)
- Trước khi build payload callback / notify, mask cùng bộ header nhạy cảm như recorder. Test: `Authorization` vắng trong payload.
- Từ chối `agent_url` non-loopback (dùng `config.is_loopback`) → raise/warn (D3).

### Timeout / disconnect / fallback (D5)
- Hết `agent_timeout` → fallback: `return_error` (504) | `pass_through` (`HandlerResult.action="pass_through"`) | `built_in`.
- Client disconnect / flow killed (signal từ phase 6) → cancel pending, KHÔNG record. `finally` luôn pop pending (chống leak).
- `max_pending` cap → khi bão hoà trả 503 ngay (không tạo thêm pending).
- `request_id = uuid4` (D5 — không content-derived; retry không ghi đè future).

### Built-in handler (nit — định nghĩa contract)
- Template mặc định: `200 {}` (hoặc echo request body nếu JSON) + `Content-Type: application/json`. Test-được.

### Payload (D-clarify — bỏ `context`)
- Payload = `{request_id, method, url, headers(masked), body, is_json, content_type}`. **Bỏ `context`** (proxy không thấy code app; LLM đã có repo).
- Content-type binary → body base64 (D8).

## Architecture
`agent_handler.py`: `AgentHandler(config, mask_headers)` với `async handle(request, on_disconnect) -> HandlerResult`, `respond(request_id, status, body, loop=None)`, `pending` dict. Sync vs pending tách method nội bộ, chọn theo `config.agent_mode`. `httpx.AsyncClient` reuse.

## Related Code Files
- Create: `src/cc_mock_server/agent_handler.py`
- Modify: `src/cc_mock_server/models.py` (HandlerResult, PendingRequest — đã khai ở phase 2)
- Create: `tests/test_agent_handler.py`

## Implementation Steps (TDD)
1. **RED** (fake agent qua `pytest-httpserver`, inject fake clock cho timeout — tránh flaky):
   - sync: callback JSON → wrap 200.
   - pending: `respond()` giải phóng future đúng request_id; request khác không ảnh hưởng.
   - **double respond** cùng id → không raise (guarded).
   - timeout mỗi fallback (`return_error`/`pass_through`/`built_in`) đúng.
   - disconnect → cancel pending, không record; `pending` dict rỗng sau (`finally`).
   - `max_pending` đạt cap → 503.
   - **`Authorization` vắng trong payload gửi agent**.
   - non-loopback `agent_url` → reject.
   - binary body → base64 trong payload.
   - built-in template đúng contract.
2. **GREEN**: implement tới pass.
3. **REFACTOR**: tách transport strategy sau interface chung.

## Success Criteria
- [ ] `pytest tests/test_agent_handler.py` pass.
- [ ] sync XOR pending rạch ròi; double/never-resolution có test.
- [ ] Timeout + disconnect + max_pending đúng; pending không leak.
- [ ] Secret không rò sang agent.
- [ ] Timeout test dùng injected clock (deterministic, không wall-clock).

## Risk Assessment
- Future leak / cross-thread set. Mitigation: `finally` pop + guard `done()` + loop-aware resolve (D1).
- Body agent trả không JSON hợp lệ. Mitigation: validate → 502 + log.
