---
phase: 8
title: "Streaming / SSE"
status: completed
priority: P2
dependencies: [6]
---

# Phase 8: Streaming / SSE support

## Overview
Cho phép mock LLM API (OpenAI/Anthropic) stream SSE: capture stream thật qua pass-through (tee), record, và replay re-emit. Scope theo D10 — priority 1+2, defer agent-composed stream, cấm real-time token streaming.

## Requirements
### Detection
- Nhận diện streaming response: `Content-Type: text/event-stream` (chính), hoặc chunked/no content-length + event-stream. Helper thuần `is_sse(headers) -> bool`.

### Capture-on-pass-through (priority 1 — cốt lõi)
- Config `capture_streams: bool = True`. Khi bật + response là SSE + đang pass-through tới upstream thật → **tee**: forward từng chunk cho app ĐỒNG THỜI buffer để record. Không được block/đợi full trước khi forward (giữ TTFT thật cho app).
- Sau khi stream kết thúc → `recorder.save` với `Response.is_stream=true`, body = raw SSE text, `content_type=text/event-stream`.
- Dùng mitmproxy streaming API (`flow.response.stream`) để tee; nếu API buộc phải buffer thì document rõ trade-off.

### Replay (priority 1 + delay priority 2)
- Replay mode + recording matched có `is_stream=true` → re-emit SSE body với `Content-Type: text/event-stream`, giữ nguyên framing (`data: ...\n\n`, `[DONE]`).
- Config `stream_delay: float = 0.0` (giây giữa mỗi SSE event khi replay; 0 = emit ngay). CLI `--stream-delay`.
- Nếu re-emit theo chunk với delay cần plumbing sâu trong mitmproxy → làm best-effort; blocker thì document + fallback emit-at-once (KHÔNG fake), như HTTPS phase 6.

### Models / recorder
- `Response.is_stream: bool = false` (D9 model đã có Response — thêm field, cập nhật round-trip). Recorder giữ nguyên cơ chế (SSE là text → không base64).

### Out of scope (defer)
- Agent-composed stream (respond `{"chunks":[...]}`) — priority 3, KHÔNG làm phase này.
- Real-time token streaming — cấm.

## Architecture
- `streaming.py`: `is_sse(headers)`, `parse_sse_events(body) -> list[str]` (tách theo `\n\n`), `frame_sse_events(events) -> bytes`. Thuần, test riêng.
- `server.py` (MockAddon): thêm streaming capture (tee via `flow.response.stream`) + streaming replay emit.
- `router.py`: quyết định capture (pass-through + SSE + capture_streams) và replay-stream.
- `config.py`: `capture_streams`, `stream_delay`. `models.py`: `Response.is_stream`.
- `help.py`/`cli.py`: `--stream-delay` param (float) cho `start`.

## Related Code Files
- Create: `src/cc_mock_server/streaming.py`, `tests/test_streaming.py`, `tests/test_streaming_integration.py`
- Modify: `src/cc_mock_server/models.py` (Response.is_stream), `config.py`, `server.py`, `router.py`, `help.py`, `cli.py`

## Implementation Steps (TDD)
1. **RED unit** (`test_streaming.py`): `is_sse` positive/negative; `parse_sse_events` tách đúng, giữ `[DONE]`; `frame_sse_events` round-trips; `Response.is_stream` round-trips qua recorder (save→load).
2. **RED integration** (`test_streaming_integration.py`, real mitmproxy + local SSE origin server):
   - live/pass-through: app hit SSE origin → nhận đủ events → recording ghi với `is_stream=true`, body = raw SSE.
   - replay: cùng request → re-emit SSE, app parse đủ events, KHÔNG chạm origin.
   - `stream_delay > 0`: events tới cách nhau (best-effort; nếu buffer-only thì assert nội dung đúng + document).
   - readiness-poll, deterministic master teardown như phase 6.
3. **GREEN**: implement.
4. **REFACTOR**: gom SSE helper.

## Success Criteria
- [ ] `pytest tests/test_streaming.py tests/test_streaming_integration.py` pass; full suite no regression.
- [ ] SSE thật capture qua pass-through → record `is_stream=true` + raw body.
- [ ] Replay re-emit SSE hợp lệ, app parse đủ events, không chạm origin.
- [ ] `--stream-delay` hoạt động hoặc blocker được document (không fake).
- [ ] Non-SSE traffic không đổi hành vi (regression).

## Risk Assessment
- mitmproxy `flow.response.stream` tee + synthetic streamed replay có thể cần low-level plumbing. Mitigation: best-effort + document blocker + fallback emit-at-once, ưu tiên correctness nội dung hơn timing.
- HTTP/2 (OpenAI/Anthropic). Mitigation: integration test dùng HTTP/1.1 SSE origin; note h2 là follow-up.
- Client hủy giữa stream. Mitigation: cleanup tee buffer, không record stream dở (chỉ record khi hoàn tất) — test client-disconnect mid-stream.
