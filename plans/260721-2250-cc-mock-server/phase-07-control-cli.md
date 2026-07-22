---
phase: 7
title: "Control API & CLI"
status: completed
priority: P2
dependencies: [6]
---

# Phase 7: Control API & CLI

## Overview
REST control API (`/mock/*`, bind 127.0.0.1) + CLI (`cc-mock`) gồm `--agent-help` structured cho LLM. Đây là interface agent dùng để poll pending + respond. Chứa test cross-loop quan trọng nhất (D1/C6).

## Requirements
### Control API (FastAPI, bind `control_bind` = 127.0.0.1 — D3)
- `GET /mock/status`, `POST /mock/mode {mode}`
- `GET/POST /mock/filter` (add/remove/list domain)
- `GET/POST /mock/select`, `DELETE /mock/select/{pattern}`
- `GET /mock/pending` (requests chờ agent — agent poll cái này, D2)
- `POST /mock/respond {request_id, status, body}` → giải phóng pending future (loop-safe, D1)
- `GET /mock/recordings`, `DELETE /mock/recordings/{id}` (id = filename stem)
- `POST /mock/config` → **từ chối `agent_url` non-loopback** (D3)
- Chia sẻ CÙNG component instances với proxy qua `app.py` composition root (không tạo bản sao state).

### CLI (`cc-mock`)
- `start` (mọi config flags: proxy-port, control-port, mode, agent-url, agent-mode, filter-mode/filter, agent-timeout, timeout-fallback, min-confidence, recordings) → khởi proxy + control API cùng process/loop (D1).
- `respond`, `select`, `deselect`, `status`, `mode`, `filter`, `recordings`, `pending` — HTTP client tới control API (`http://127.0.0.1:{control_port}`).
- `--agent-help` → markdown structured (mục đích command, param format + ví dụ, response shape, error codes, workflow poll→respond). Sinh từ command registry (single source → chống drift).

## Architecture
`control_api.py`: FastAPI nhận `app.py` composition root (shared state). `cli.py` + `__main__.py`: argparse/click; subcommands gọi control API qua httpx. `help.py`: sinh agent-help từ command registry. Proxy master + uvicorn cùng loop (D1).

## Related Code Files
- Create: `src/cc_mock_server/control_api.py`, `cli.py`, `help.py`, `__main__.py`
- Modify: `pyproject.toml` (`[project.scripts] cc-mock = "cc_mock_server.__main__:main"`)
- Create: `tests/test_control_api.py`, `tests/test_cli.py`, `tests/test_cross_loop.py`
- Modify: `README.md` (usage đầy đủ + HTTPS CA setup + streaming/scope notes — Global AC/L1)

## Implementation Steps (TDD)
1. **RED control API** (TestClient): mode switch đổi router mode; filter add/remove phản ánh DomainFilter; select/deselect; recordings list/delete; **`POST /mock/config` với agent_url non-loopback → 400**.
2. **RED cross-loop (D1/C6 — test quan trọng nhất)** `tests/test_cross_loop.py`: khởi proxy + control API qua `app.py` bootstrap THẬT trên 1 loop; fire app request (pending) qua proxy; từ client khác `POST /mock/respond`; assert app request unblock đúng body + recording được ghi. (Không dùng TestClient loop cô lập.)
3. **RED CLI**: subcommands trỏ fake control API → assert HTTP call đúng; `--agent-help` chứa mọi command + ví dụ; **drift test**: help registry khớp subcommands thực.
4. **GREEN**: implement api + cli + help.
5. **REFACTOR**: command metadata một nguồn cho argparse + agent-help.

## Success Criteria
- [x] `pytest tests/test_control_api.py tests/test_cli.py tests/test_cross_loop.py` pass. (37 passed)
- [x] `cc-mock start` chạy proxy + control API (bind 127.0.0.1); `cc-mock status` OK. (verified manually via installed console script, incl. real proxy intercept -> pending -> respond -> recording e2e)
- [x] `cc-mock respond --request-id X --json '{...}'` unblock pending (cross-loop test xanh).
- [x] `POST /mock/config` chặn agent_url non-loopback.
- [x] `cc-mock --agent-help` đủ command/param/ví dụ/error code; drift test pass.
- [x] README có trust-CA HTTPS + streaming-scope note.

Full suite: `pytest -q` -> 243 passed (206 pre-existing + 15 control_api + 1 cross_loop + 21 cli), no regressions.

Deviation from spec: fixed a real Ctrl-C shutdown hang discovered during manual e2e verification -- `uvicorn.Server.serve()` installs its own SIGINT/SIGTERM handler and returns on signal, but a plain `asyncio.gather(master.run(), server.serve())` would then wait forever on `master.run()` alone (process leak). `cli._run_start` now runs both as tasks and uses `asyncio.wait(..., FIRST_COMPLETED)` so either half finishing triggers `shutdown(application)` for both -- still one loop, still both coroutines scheduled together (D1), just correct on signal-driven shutdown too.

## Risk Assessment
- 2 server 1 process. Mitigation: cùng loop, `asyncio.gather(master.run(), server.serve())` (D1).
- Agent-help drift. Mitigation: sinh từ registry + drift test.
- Cross-loop resolution sai (silent hang). Mitigation: test_cross_loop qua bootstrap thật là gate bắt buộc.
