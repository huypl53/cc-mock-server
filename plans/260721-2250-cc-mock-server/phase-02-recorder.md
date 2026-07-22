---
phase: 2
title: "Models & Recorder Store"
status: pending
priority: P1
dependencies: [1]
---

# Phase 2: Models & Recorder Store

## Overview
Định nghĩa TOÀN BỘ model surface up-front (D9) + lưu/load mỗi request-response pair thành 1 file JSON theo hostname. Data layer cho record (live) và load (replay).

## Requirements
### Models (D9 — định nghĩa hết ở đây để tránh churn)
- `Request{method, url, host, path, query, headers, body, is_json, content_type}`
- `Response{status_code, headers, body, is_json, content_type}`
- `Recording{id, request, response, metadata}`; metadata `{recorded_at, source, fuzzy_key}`
- `HandlerResult{action: Literal["respond","pass_through"], response: Response | None}` (phase 5 dùng)
- `PendingRequest{request_id, request, future, created_at}` (phase 5 dùng)

### Recorder
- `save(request, response) -> Recording`. File dưới `recordings/{safe_host}/{METHOD}_{path_slug}_{ts}_{shortuuid}.json` (D5 — random suffix tránh collision).
- `id = filename stem` (stable, URL-safe) — dùng cho `DELETE /mock/recordings/{id}` (H4: chốt stem, KHÔNG content-hash để giữ history).
- `load_all() -> list[Recording]`; file hỏng → skip + log warning.
- `delete(id)`, `list()`.
- **Masking (D3)**: `Authorization`, `api-key`, `x-api-key`, `cookie`, `set-cookie` (config-driven set) → `***` khi ghi. Test: không plaintext trên đĩa.
- **Path safety (H3)**: sanitize `host` (Host header có thể chứa `../`), slug hoá path (strip query, ký tự đặc biệt), cap tổng filename ≤ 255 bytes, `resolve()` và confine dưới `recordings_dir` (reject path escape).
- **Content-type (D8)**: body không JSON/text → lưu base64 + `is_json=false` + `content_type`.
- **In-memory ownership (D6)**: Recorder giữ list recordings; expose snapshot cho matcher; `save/delete` mutate qua `asyncio.Lock`.
- **fuzzy_key (H4)**: KHÔNG tính ở đây (circular với phase 3). Lưu `fuzzy_key=None` lúc save; backfill/inject `matcher.fuzzy_key` callable ở composition root (phase 6). Model cho phép None.

## Architecture
`models.py` (toàn bộ trên) + `recorder.py`: `Recorder(recordings_dir, mask_headers, lock)`. Masking + path-safety là helper thuần, test riêng.

## Related Code Files
- Create: `src/cc_mock_server/models.py`, `src/cc_mock_server/recorder.py`
- Create: `tests/test_recorder.py`, `tests/test_models.py`

## Implementation Steps (TDD)
1. **RED** (dùng `tmp_path`): save tạo đúng cấu trúc host dir; round-trip save→load giữ body; corrupt file skip; sensitive headers masked; slug an toàn cho URL có query/`/`; **host `../../x` không escape recordings_dir**; filename dài bị cap; binary body → base64 + is_json=false; `id` == filename stem ổn định; concurrent save+delete không lỗi (async lock).
2. **GREEN**: `models.py` + `recorder.py` tới pass.
3. **REFACTOR**: masking + path-safe helper tách module `sanitize.py`.

## Success Criteria
- [ ] `pytest tests/test_recorder.py tests/test_models.py` pass.
- [ ] Round-trip giữ nguyên request/response.
- [ ] Sensitive headers không plaintext on-disk.
- [ ] Host/path traversal bị chặn (confined dưới recordings_dir).
- [ ] Binary body lưu base64.
- [ ] `id` stable = filename stem.

## Risk Assessment
- Filename collision. Mitigation: short-uuid suffix.
- Circular dep recorder↔matcher. Mitigation: fuzzy_key nullable, inject callable ở phase 6.
