# Brainstorm Report: CC Mock Server

- **Date**: 2026-07-21
- **Status**: Design approved, ready for planning
- **Modes**: standard brainstorm (no --html, no --wiki)

## Problem Statement

AI coding agents phát triển app dùng third-party APIs gặp vấn đề: API có thể không available, tốn tiền, rate-limited, hoặc trả data không phù hợp cho debugging. Cần hệ thống proxy thông minh cho phép AI agent tự compose mock responses dựa trên context của code, record lại để replay sau → app luôn có "third-party API hoạt động" mà không phụ thuộc API thật.

## Requirements (chốt)

| # | Item | Quyết định |
|---|------|-----------|
| Output | Python package `cc-mock-server` với CLI + proxy server + control API | ✅ |
| Agent comms | Mini HTTP callback server (external) + built-in handler — cả hai | ✅ |
| Proxy/Gateway | Gộp thành 1 process | ✅ |
| Request matching (replay) | Fuzzy match: URL pattern + method, bỏ dynamic fields | ✅ |
| App routing | `HTTP_PROXY` env var | ✅ |
| Recording format | Từng file JSON, tổ chức theo hostname | ✅ |
| Agent response | Raw JSON body (agent nhìn code + request → soạn JSON) | ✅ |
| Domain filter | Whitelist/blacklist domains/IP, wildcard | ✅ |
| Timeout | Configurable timeout + fallback strategy | ✅ |
| CLI help | `--agent-help` structured cho LLM đọc | ✅ |
| Endpoint selection | Agent runtime chọn service/endpoint để handle | ✅ |

## Kiến trúc

```
App ──HTTP_PROXY──▶ CC Mock Server (Proxy + Gateway gộp) ──▶ App
                        │
      Proxy → Filter → Selector → Mode Router (Live/Replay)
                                       │
                          Agent Handler (Built-in | External callback)
                                       │
                                  Recorder (JSON files)
```

### Request pipeline (priority logic)

```
Request đến
  [1] Domain trong user filter?      No ─▶ Pass-through (real API)
  [2] Agent đã select endpoint?      No ─▶ Replay nếu có recording, else pass-through
  [3] Gửi tới Agent Handler
  [4] Agent response? Timeout ─▶ Fallback (return_error | pass_through | built_in)
  [5] Record + trả về App
```

## Components

1. **Proxy Server** — `localhost:8080`, xử lý HTTP CONNECT tunneling + plain HTTP, dùng `mitmproxy`.
2. **Filter** — whitelist/blacklist domain/IP, wildcard. User config ban đầu.
3. **Selector** — agent runtime chọn endpoints để handle (khác filter: agent tự quyết).
4. **Mode Router** — `live` (intercept→agent→record) vs `replay` (match recording→trả về, fallback live).
5. **Agent Handler**:
   - Built-in: rules/templates đơn giản.
   - External callback: POST request details tới `agent_callback_url`, nhận raw JSON.
6. **Recorder** — mỗi req/res = 1 JSON file dưới `recordings/{host}/{method}_{path}_{ts}.json`.
7. **Fuzzy Matcher** — `fuzzy_key = METHOD::host::path_pattern`, normalize path params (`/users/123`→`/users/{id}`), sort query keys, so structure của body, chọn recording gần nhất.
8. **Control API** — REST prefix `/mock/` để quản lý mode/filter/select/recordings/pending.
9. **CLI** — start server + `respond`/`select`/`deselect` + `--agent-help`.

## Timeout & Fallback

- Default timeout 30s.
- Fallback strategies: `return_error` (504, default) | `pass_through` (forward real API) | `built_in` (dùng built-in handler).

## Tech Stack

- Python 3.11+
- `mitmproxy` — HTTP/HTTPS proxy (CA cert built-in cho HTTPS interception)
- `fastapi` hoặc `aiohttp` — Control API
- Filesystem + JSON — không cần DB

## File Structure

```
cc-mock-server/
├── pyproject.toml
├── README.md
├── src/cc_mock_server/
│   ├── __main__.py       # CLI entry
│   ├── server.py         # Proxy server
│   ├── router.py         # Mode router
│   ├── filter.py         # Domain/IP filter
│   ├── selector.py       # Agent endpoint selection
│   ├── agent_handler.py  # Built-in + external callback
│   ├── recorder.py       # Recording store
│   ├── matcher.py        # Fuzzy matching
│   ├── control_api.py    # REST control API
│   ├── help.py           # Agent-friendly help
│   └── config.py
├── recordings/
└── tests/
```

## Control API

| Endpoint | Mô tả |
|----------|--------|
| `GET /mock/status` | Trạng thái + mode hiện tại |
| `POST /mock/mode` | Switch live/replay |
| `GET/POST /mock/filter` | Domain filter (user config) |
| `GET/POST /mock/select` | Agent chọn endpoints handle |
| `DELETE /mock/select/{pattern}` | Bỏ handle endpoint |
| `GET /mock/pending` | Requests đang chờ agent |
| `GET /mock/recordings` | List recordings |
| `DELETE /mock/recordings/{id}` | Xoá recording |
| `POST /mock/config` | Update config runtime |

## CLI

```bash
cc-mock start --port 8080 --mode live --agent-url http://localhost:9999/handle \
  --filter-mode whitelist --filter "api.stripe.com,api.openai.com" \
  --agent-timeout 30 --timeout-fallback pass_through
cc-mock start --mode replay --recordings ./recordings
cc-mock respond --request-id <id> --status 200 --json '{"id": 123}'
cc-mock select --endpoints "GET api.stripe.com/v1/charges"
cc-mock deselect --endpoints "POST api.stripe.com/v1/refunds"
cc-mock --agent-help      # structured help cho LLM
```

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| HTTPS interception cần CA cert | `mitmproxy` built-in CA, document setup |
| Agent timeout → app treo | Timeout + fallback strategy |
| Recording phình to | Cleanup policy / max per host |
| Fuzzy match sai | Log match confidence, review qua Control API |

## Acceptance Criteria

1. `HTTP_PROXY=http://localhost:8080` → requests đi qua server.
2. Live mode: intercept → agent → response → record.
3. Replay mode: fuzzy match recording → trả về, không gọi agent.
4. External agent nhận POST request details, trả raw JSON → server wrap thành HTTP response.
5. Recordings = từng file JSON theo hostname.
6. Filter whitelist/blacklist hoạt động (pass-through cho domain ngoài whitelist).
7. Agent select/deselect endpoints runtime.
8. Timeout → fallback đúng strategy đã config.
9. `--agent-help` output structured markdown.
10. Control API: switch mode, list/delete recordings, manage filter/select.

## Next Steps

- Lập implementation plan (`/ck:plan`) → chia phase: proxy core → filter/selector → agent handler → recorder/matcher → control API → CLI/help.
