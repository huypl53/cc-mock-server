---
title: "CC Mock Server"
status: completed
created: 2026-07-21
updated: 2026-07-22
result: "phases 1-9 + init done; 304 tests pass. Agent-composed SSE UAT'd through the CLI."
mode: tdd
source: docs/brainstorm-cc-mock-server-2026-07-21.md
review: validate + red-team applied (2026-07-22)
blockedBy: []
blocks: []
---

# Plan: CC Mock Server

Smart mocking proxy cho AI coding agents. App route qua `HTTP_PROXY` → server intercept third-party API calls → AI agent (external callback hoặc pending/respond) hoặc built-in handler compose JSON response → record → replay sau bằng fuzzy match.

Brainstorm gốc: `docs/brainstorm-cc-mock-server-2026-07-21.md`

## Mode: TDD

Mỗi phase viết test trước (RED) → implement tới khi pass (GREEN) → refactor. Framework: `pytest` + `pytest-asyncio` + `pytest-httpserver`.

---

## Cross-Cutting Decisions (CHỐT trước khi cook — kết quả review)

Đây là các quyết định load-bearing mà validate + red-team yêu cầu chốt. Mọi phase tuân theo mục này.

### D1. Concurrency / event-loop model (rủi ro #1)
- **Primary: single event loop.** `app.py` composition root chạy `asyncio.gather(master.run(), uvicorn_server.serve())` trên CÙNG 1 loop. mitmproxy 9+/11 là asyncio-native (`Master.run()` là coroutine), uvicorn `Server.serve()` là coroutine → không tranh quyền `run_until_complete`.
- **Pending Future** tạo bằng `asyncio.get_running_loop().create_future()`. Vì mitmproxy xử lý mỗi flow trong 1 task riêng, một flow `await` Future lâu **không** block flow khác hay control API → loop vẫn phục vụ `POST /mock/respond` để resolve Future.
- **Fallback bắt buộc nếu uvicorn buộc phải chạy thread riêng**: resolve Future qua `master_loop.call_soon_threadsafe(fut.set_result, ...)`. KHÔNG bao giờ gọi `set_result` cross-thread trực tiếp.
- **Bắt buộc test** (phase 7): resolve pending từ control-API path và assert flow coroutine wake — chạy qua `app.py` bootstrap thật, không phải TestClient loop cô lập.
- **Version guard**: verify mitmproxy hỗ trợ Python target; nếu không, cap dev xuống 3.12/3.13 (xem phase 1).

### D2. Agent transport: sync XOR pending (không trộn)
Chọn per-request bằng config `agent_mode`:
- `pending` (**default, primary cho LLM agent**): callback (nếu có `agent_url`) là fire-and-forget/202; resolution CHỈ qua `POST /mock/respond`. Agent poll `GET /mock/pending` → `respond`.
- `sync`: `await httpx.post(agent_url)` → dùng body trả về inline; KHÔNG tạo pending Future.
- Guard mọi `set_result` bằng `if not fut.done()`. Test cả race "callback cũng respond" và "double respond".

### D3. Secret handling + control API lockdown (bảo mật)
- Header nhạy cảm (`Authorization`, `api-key`, `x-api-key`, `cookie`, ...) mask **cả** khi record **và** trước khi gửi payload callback tới agent.
- Control API bind `127.0.0.1` mặc định (config `control_bind`). `POST /mock/config` từ chối `agent_url` non-loopback (hoặc warn loud + require flag).
- Test: `Authorization` vắng mặt trong callback body và trong recording trên đĩa.

### D4. Fuzzy match confidence gating
`match()` trả `None` khi confidence < `min_confidence` (config) → đi fallback thay vì trả recording sai âm thầm. Đây là success-criteria phase 3, không phải log line.

### D5. Blocking semantics + orphaned pendings
- App request bị giữ tới `agent_timeout`. Default `agent_timeout = 10s` (< client timeout phổ biến).
- Detect client disconnect / flow killed → cancel pending, **KHÔNG record** (tránh poison replay).
- `request_id` = uuid4 (không content-derived) để tránh retry ghi đè future. `max_pending` cap → khi bão hoà trả 503.
- `finally` luôn pop pending (không leak).

### D6. Shared mutable state
- Single-loop → dùng `asyncio.Lock`. Snapshot `mode`/filter/selector tại flow-entry (flow đã vào `live` không đổi hành vi giữa chừng khi có `POST /mock/mode`).
- Recordings in-memory: 1 owner (`Recorder`), `match()` iterate trên bản copy/snapshot; `save`/`delete` mutate qua owner có lock.

### D7. HTTPS là first-class (không chỉ doc)
- Filter quyết định ở stage `tls_clienthello`/CONNECT — domain out-of-filter KHÔNG bị TLS-terminate (tránh vỡ cert-pinning + không cần trust CA cho host pass-through).
- ≥1 integration test HTTPS: client `verify=<mitmproxy_ca.pem>` → intercept→record→replay over TLS (phase 6). Nâng lên Global Acceptance.

### D8. Content-type awareness (không giả định JSON)
- Body không phải JSON/text (binary/gzip/multipart) → lưu base64 + `is_json=false` + `content_type`. Callback payload dùng base64 cho binary.
- `matcher.body_structure` chỉ áp cho JSON; non-JSON bỏ qua body tie-break.
- Content-type không support cho compose → `pass_through` (state rõ).

### D10. Streaming / SSE (Phase 8)
LLM API (OpenAI/Anthropic) stream SSE mặc định → mock buffered không chạy qua stream-handling code của app. Scope Phase 8:
- **Làm**: detect `text/event-stream`; **capture-on-pass-through** (tee: forward chunk cho app đồng thời buffer để record); record raw SSE (là text → lưu string, `is_stream=true`); replay re-emit SSE body với content-type đúng + optional `stream_delay` giữa events.
- **Phase 9 (priority 3)**: agent-composed stream — agent respond `{"stream":true,"chunks":[...]}` với `chunks` là list SSE event strings **đã framed sẵn** (direction B: agent-agnostic, cc-mock chỉ `frame_sse_events` nối `\n\n`, KHÔNG hiểu payload → hỗ trợ cả OpenAI/Anthropic/bất kỳ). Emitted qua injected-response path (1 frame, `stream_delay` no-op như replay), recorded `is_stream=true` → replayable. **Atomic**: tất cả chunks trong 1 respond, KHÔNG real-time.
- **Cấm**: real-time agent-token-streaming (rabbit hole, giá trị test thấp).
- **Lý do dùng pass-through-capture, KHÔNG pending**: pending giữ connection chờ agent rồi mới stream → xung đột bản chất long-lived. Pass-through né hẳn vấn đề contract — app chạm API thật 1 lần, cc-mock tee + record, lần sau replay.
- Matcher KHÔNG đụng (match trên request JSON). Nếu mitmproxy stream-delay cần plumbing sâu → document blocker, không fake (như HTTPS phase 6).

### D9. Models up-front (tránh churn)
Định nghĩa toàn bộ model surface (`Request`, `Response`, `Recording`, `HandlerResult`, `PendingRequest`) ngay phase 2 `models.py` để TDD không phải rewrite test phase sớm.

---

## Architecture

```
App ──HTTP_PROXY──▶ Proxy (mitmproxy addon)
   tls_clienthello: Filter.should_intercept(host)? no ─▶ pass-through (không TLS-terminate)
   request hook (intercepted):
      snapshot mode/filter/selector
      Selector.is_selected? ──┐
      Mode Router ────────────┤
        replay: Matcher.match (confidence-gated) → recording | miss→replay_miss_strategy
        live+selected: AgentHandler.handle (sync|pending, timeout→fallback) → Recorder.save
   response → App
Control API (FastAPI, 127.0.0.1) ── shares component instances via app.py composition root
```

Request pipeline priority (intercepted flows):
```
[0] (CONNECT/tls) domain trong filter?  No ─▶ pass-through (no decrypt)
[1] snapshot state
[2] mode == replay → matcher.match (gated); hit→recording; miss→replay_miss_strategy
[3] mode == live + selector.is_selected → agent_handler.handle; else replay/pass_through
[4] agent timeout/disconnect → fallback (return_error|pass_through|built_in); disconnect→no record
[5] live thành công → recorder.save (masked)
```

## Phases

| # | Phase | Priority | Depends | File |
|---|-------|----------|---------|------|
| 1 | Scaffold & Config | P1 | — | phase-01-scaffold-config.md |
| 2 | Models & Recorder Store | P1 | 1 | phase-02-recorder.md |
| 3 | Fuzzy Matcher | P1 | 1,2 | phase-03-matcher.md |
| 4 | Filter & Selector | P1 | 1 | phase-04-filter-selector.md |
| 5 | Agent Handler | P1 | 1,2 | phase-05-agent-handler.md |
| 6 | Proxy & Mode Router | P1 | 2,3,4,5 | phase-06-proxy-router.md |
| 7 | Control API & CLI | P2 | 6 | phase-07-control-cli.md |
| 8 | Streaming / SSE | P2 | 6 | phase-08-streaming.md |
| 9 | Agent-composed SSE stream | P3 | 5,8 | phase-09-agent-stream.md |

## Tech Stack

- Python 3.11+ (verify mitmproxy hỗ trợ version target — xem D1)
- `mitmproxy` — HTTP/HTTPS proxy addon (asyncio-native)
- `fastapi` + `uvicorn` — Control API (bind 127.0.0.1)
- `httpx` — external agent callback client
- `pydantic` — config + models
- `pytest`, `pytest-asyncio`, `pytest-httpserver`

## Global Acceptance Criteria

1. `HTTP_PROXY=http://localhost:8080` → requests đi qua server.
2. Live mode: intercept → agent (sync|pending) → response → record.
3. Replay mode: fuzzy match (confidence-gated) → trả về, không gọi agent; low-confidence → fallback.
4. External agent nhận POST request details, trả JSON → server wrap thành HTTP response.
5. Recordings = từng file JSON theo hostname; sensitive headers masked trên đĩa.
6. Filter whitelist/blacklist ở CONNECT stage (domain ngoài scope không bị TLS-terminate).
7. Agent select/deselect endpoints runtime.
8. Timeout/disconnect → fallback đúng strategy; disconnect không tạo recording.
9. `cc-mock --agent-help` output structured markdown.
10. Control API (127.0.0.1): switch mode, list/delete recordings, manage filter/select, respond pending.
11. **HTTPS**: ≥1 e2e test intercept→record→replay over TLS với mitmproxy CA.
12. **Security**: `Authorization` không xuất hiện trong callback payload lẫn recording on-disk.
13. **Cross-loop**: pending resolve qua control API unblock flow đang chờ (test qua bootstrap thật).

## Out of Scope (v1)

- WebSocket mocking (SSE `text/event-stream` streaming IS supported — phase 8/D10: pass-through tee-capture + replay; agent-composed streams + real-time token streaming remain deferred/forbidden).
- Auth/multi-tenant cho control API (chỉ bind loopback).
- Web dashboard UI.
- Recording auto-cleanup/rotation (note hard cap dù defer — tránh cạn inode).
