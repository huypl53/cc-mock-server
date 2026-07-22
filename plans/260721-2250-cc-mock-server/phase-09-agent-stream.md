---
phase: 9
title: "Agent-composed SSE stream"
status: completed
priority: P3
dependencies: [5, 8]
---

# Phase 9: Agent-composed SSE stream (D10 priority 3, direction B)

## Overview
Cho agent tự compose 1 SSE stream response (không cần upstream thật) bằng cách
respond `{"stream": true, "chunks": [...]}`. `chunks` là list SSE event strings
**đã framed sẵn** — cc-mock chỉ nối `\n\n` (direction B: agent-agnostic, không
hiểu payload → OpenAI/Anthropic/bất kỳ đều được). Emitted qua injected-response
path, recorded `is_stream=true` → replay y hệt phase 8.

## Requirements
### Contract (envelope — dùng chung sync callback return + POST /mock/respond + CLI)
```json
{"stream": true, "chunks": ["data: {\"delta\":\"hi\"}", "data: [DONE]"], "status": 200, "headers": {}}
```
- `chunks: list[str]` present → streaming response. Mỗi phần tử là 1 SSE event
  đã framed (agent tự lo `data:`/`event:`/`[DONE]`). cc-mock:
  `body = frame_sse_events(chunks).decode()`, `is_stream=true`, `is_json=false`,
  `content_type = content_type/headers override HOẶC "text/event-stream"`,
  `status` default 200.
- `stream: true` là cờ tường minh; nhưng **`chunks` (list) là dấu hiệu quyết
  định** (stream iff `isinstance(chunks, list)`). `chunks` không phải list →
  malformed (502 sync / 422 pydantic control API).
- Empty `chunks` `[]` → body rỗng, vẫn hợp lệ (agent's choice).

### Behavior
- **Atomic, KHÔNG real-time**: agent gửi tất cả chunks trong 1 respond; cc-mock
  phát body 1 lần. `stream_delay` vẫn là documented no-op (cùng injected-response
  blocker mitmproxy như phase 8 replay — KHÔNG fake).
- **Recorded**: đi qua live path `_save_recording` như mọi respond → ghi
  `is_stream=true`, replay lần sau qua matcher/phase-8 path (không cần agent).
- **Không đụng** capture-on-pass-through phase 8 (đó là stream THẬT từ upstream);
  đây là stream do agent bịa.

### CLI
- `cc-mock respond --request-id X --chunk 'data: {...}' --chunk 'data: [DONE]'`.
- `--chunk` repeatable (action=append) là **dấu hiệu trigger duy nhất**: có
  `--chunk` → gửi `{"stream":true,"chunks":[...]}` trong payload, `--json` KHÔNG
  còn bắt buộc. (Đã BỎ `--stream` flag ở CLI để khỏi phải thêm `store_true` vào
  ParamSpec registry — chunks là authoritative; envelope HTTP vẫn mang `stream:true`.)
- `--json` + `--chunk` cùng lúc → lỗi rõ ràng (mutually exclusive).

## Architecture
- `agent_handler.py`: `_make_response(..., chunks=None)` — khi `chunks` là list →
  build streaming Response qua `streaming.frame_sse_events`, bỏ qua `body`.
  `respond()` thêm param `chunks`; `_response_from_agent_json` đọc
  `data.get("chunks")` + `data.get("stream")`, validate, truyền xuống.
- `models.py`: `Response.from_chunks(chunks, *, status_code=200, headers=None,
  content_type=None)` helper (frame + is_stream=True) — 1 chỗ build, dễ test.
- `control_api.py`: `RespondRequest` thêm `stream: bool = False`,
  `chunks: Optional[list[str]] = None`; endpoint truyền `chunks` xuống
  `agent_handler.respond`.
- `cli.py`: `cmd_respond` thêm `--stream`/`--chunk`; `--json` optional khi có
  chunks; validate mutually-exclusive; payload gồm `stream`/`chunks`.
- `help.py`: respond CommandSpec thêm `--stream`/`--chunk` ParamSpec + document
  envelope trong `render_agent_help`.

## Related Code Files
- Modify: `src/cc_mock_server/agent_handler.py`, `models.py`, `control_api.py`,
  `cli.py`, `help.py`
- Create: `tests/test_agent_stream.py` (+ mở rộng
  `tests/test_streaming_integration.py` cho e2e nếu gọn)

## Implementation Steps (TDD)
1. **RED unit** (`test_agent_stream.py`):
   - `Response.from_chunks([...])` → body framed đúng, is_stream=True,
     content_type text/event-stream, round-trips qua parse_sse_events.
   - `agent_handler.respond(id, 200, None, chunks=[...])` resolve future với
     streaming Response; sync `_response_from_agent_json({"stream":true,"chunks":[...]})`
     tương tự; `chunks` không phải list → malformed 502.
   - CLI `cmd_respond` với `--chunk` build payload đúng (chunks, no body);
     `--json`+`--chunk` → exit != 0; drift test vẫn pass (help registry).
2. **RED integration** (real mitmproxy, pending mode): app request SSE endpoint
   (selected) → agent respond chunks → app nhận SSE body đủ events + content-type
   text/event-stream → recording `is_stream=true`. Rồi replay mode: cùng request
   re-emit không cần agent.
3. **GREEN** implement. 4. **REFACTOR**.

## Success Criteria
- [ ] Agent respond `{"stream":true,"chunks":[...]}` → app nhận SSE hợp lệ,
      content-type text/event-stream, đủ events.
- [ ] Recorded `is_stream=true`; replay re-emit không cần agent.
- [ ] Direction B: chunks framed verbatim (OpenAI `data:`+`[DONE]` VÀ Anthropic
      `event:` cùng round-trip; test cả hai shape).
- [ ] CLI `--stream --chunk` hoạt động; `--json`+`--chunk` mutually exclusive.
- [ ] `stream_delay` no-op được giữ nguyên (không fake); non-stream respond
      không đổi hành vi (regression: 288 tests giữ nguyên).

## Risk Assessment
- Trộn chunks-path vào `_make_response` có thể làm rối body-path. Mitigation:
  early-return khi `chunks is not None`, không đụng nhánh body cũ.
- Agent gửi chunks CHƯA framed (thiếu `data:`): cc-mock KHÔNG sửa (direction B,
  agent-agnostic) — document rõ trong --agent-help rằng chunks phải là event đã
  framed. Không validate nội dung SSE (đúng tinh thần "không hiểu payload").
