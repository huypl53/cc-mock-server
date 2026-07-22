---
phase: 1
title: "Scaffold & Config"
status: pending
priority: P1
dependencies: []
---

# Phase 1: Scaffold & Config

## Overview
Dựng Python package skeleton, pyproject, test harness, và config model (Pydantic) load từ CLI args / YAML / env với defaults. Config phải chứa mọi field mà các phase sau dựa vào (xem Cross-Cutting Decisions trong plan.md).

## Requirements
- Functional: `Config` gồm:
  - `proxy_port: int = 8080`
  - `control_port: int = 8081`
  - `control_bind: str = "127.0.0.1"` (D3)
  - `mode: Literal["live","replay"] = "live"`
  - `agent_url: str | None = None`
  - `agent_mode: Literal["sync","pending"] = "pending"` (D2 — default pending)
  - `agent_timeout: float = 10.0` (D5 — < client timeout phổ biến)
  - `timeout_fallback: Literal["return_error","pass_through","built_in"] = "return_error"`
  - `replay_miss_strategy: Literal["pass_through","live","return_error"] = "pass_through"` (D-clarify)
  - `min_confidence: float = 0.6` (D4)
  - `max_pending: int = 100` (D5)
  - `filter_mode: Literal["whitelist","blacklist"] = "whitelist"`
  - `filter_domains: list[str] = []`
  - `recordings_dir: Path = Path("recordings")`
- Functional: load ưu tiên CLI > env (`CC_MOCK_*`) > YAML file > default.
- Functional: validators — enums hợp lệ; `agent_timeout > 0`; `0 <= min_confidence <= 1`; `max_pending > 0`; domains normalize lowercase; nếu `agent_mode == "sync"` thì `agent_url` bắt buộc (raise nếu thiếu).
- Functional: helper `is_loopback(url) -> bool` để phase 3/5/7 chặn `agent_url` non-loopback (D3).
- Non-functional: `pytest` chạy được; import `cc_mock_server` OK. Verify mitmproxy hỗ trợ Python target (D1) — document trong README nếu phải cap 3.12/3.13.

## Architecture
`config.py`: `Config(BaseModel)` + `load_config(cli_overrides, yaml_path, env)`. Enums tách `enums.py` nếu tái dùng. `is_loopback()` parse host, so với `{127.0.0.1, ::1, localhost}`.

## Related Code Files
- Create: `pyproject.toml` (deps core: mitmproxy, fastapi, uvicorn, httpx, pydantic; dev: pytest, pytest-asyncio, pytest-httpserver)
- Create: `src/cc_mock_server/__init__.py`, `config.py`, `enums.py`
- Create: `tests/__init__.py`, `tests/conftest.py`, `tests/test_config.py`
- Create: `README.md` (stub — usage + HTTPS CA + streaming/scope notes bổ sung ở phase 7)

## Implementation Steps (TDD)
1. **RED** `tests/test_config.py`: defaults đúng cho mọi field mới; enum invalid raise; precedence CLI>env>yaml>default; domain normalize lowercase; `agent_timeout<=0` raise; `min_confidence` ngoài [0,1] raise; `agent_mode=sync` thiếu `agent_url` raise; `is_loopback` đúng cho 127.0.0.1/::1/localhost và sai cho domain ngoài.
2. **GREEN**: `pyproject.toml`, `enums.py`, `config.py` tới pass.
3. **REFACTOR**: gom validator, freeze default constants.

## Success Criteria
- [ ] `pip install -e ".[dev]"` thành công.
- [ ] `pytest tests/test_config.py` pass.
- [ ] Mọi field ở Cross-Cutting Decisions tồn tại + validate.
- [ ] `is_loopback` có test dương/âm.

## Risk Assessment
- mitmproxy transitive deps nặng / version support. Mitigation: verify Python compat sớm; cô lập proxy deps nếu CI cần nhanh.
