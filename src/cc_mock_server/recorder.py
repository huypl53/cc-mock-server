"""Record/replay data store (plan.md phase 2).

`Recorder` persists each live request/response pair as one JSON file under
`recordings_dir/{safe_host}/`, and loads them back for replay (phase 3+).

Ownership (D6): `Recorder` is the single owner of the in-memory recordings
list. `save`/`delete` mutate it under an `asyncio.Lock`; `snapshot()` hands
the matcher (phase 3) an immutable tuple so it never iterates a mutating
collection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from pydantic import ValidationError

from cc_mock_server.models import Recording, RecordingMetadata, Request, Response
from cc_mock_server.sanitize import (
    DEFAULT_SENSITIVE_HEADERS,
    build_filename,
    confine_path,
    mask_headers,
    sanitize_host,
    slugify_path,
)

logger = logging.getLogger(__name__)

#: Filename glob used to discover recording files under `recordings_dir`.
_RECORDING_GLOB = "*.json"


class Recorder:
    """Owns recording persistence + the in-memory recordings list (D6)."""

    def __init__(
        self,
        recordings_dir: Path | str,
        mask_headers_set: Iterable[str] = DEFAULT_SENSITIVE_HEADERS,
        *,
        lock: Optional[asyncio.Lock] = None,
    ) -> None:
        self._recordings_dir = Path(recordings_dir)
        self._recordings_dir.mkdir(parents=True, exist_ok=True)
        self._mask_headers = frozenset(name.lower() for name in mask_headers_set)
        self._lock = lock if lock is not None else asyncio.Lock()
        # id -> Recording / id -> on-disk path, kept in lockstep.
        self._recordings: dict[str, Recording] = {}
        self._paths: dict[str, Path] = {}

    @property
    def recordings_dir(self) -> Path:
        return self._recordings_dir

    def load_all(self) -> list[Recording]:
        """(Re)load every recording file under `recordings_dir`.

        Corrupt files (invalid JSON or schema mismatch) are skipped with a
        logged warning rather than raising — one bad file must not prevent
        the rest of the recordings from loading. Replaces the in-memory
        state; intended to be called once at startup before concurrent
        `save`/`delete` traffic begins.
        """
        recordings: dict[str, Recording] = {}
        paths: dict[str, Path] = {}
        for path in sorted(self._recordings_dir.rglob(_RECORDING_GLOB)):
            try:
                raw = path.read_text(encoding="utf-8")
                data = json.loads(raw)
                recording = Recording.model_validate(data)
            except (json.JSONDecodeError, ValidationError, OSError, UnicodeDecodeError) as exc:
                logger.warning("skipping corrupt recording file %s: %s", path, exc)
                continue
            recordings[recording.id] = recording
            paths[recording.id] = path
        self._recordings = recordings
        self._paths = paths
        return list(recordings.values())

    async def save(self, request: Request, response: Response, *, source: str = "live") -> Recording:
        """Mask sensitive headers, write a new recording file, and return it.

        `fuzzy_key` is always written as `None` (H4) — computing it here
        would create a circular import with `matcher.py` (phase 3); the
        composition root (phase 6) backfills it via an injected callable.
        """
        masked_request = request.model_copy(
            update={"headers": mask_headers(request.headers, self._mask_headers)}
        )
        masked_response = response.model_copy(
            update={"headers": mask_headers(response.headers, self._mask_headers)}
        )

        async with self._lock:
            safe_host = sanitize_host(request.host)
            host_dir = confine_path(self._recordings_dir, safe_host)
            host_dir.mkdir(parents=True, exist_ok=True)

            slug = slugify_path(request.path)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            suffix = uuid.uuid4().hex[:8]
            filename = build_filename(request.method, slug, timestamp, suffix)
            file_path = confine_path(self._recordings_dir, safe_host, filename)

            recording_id = file_path.stem
            metadata = RecordingMetadata(
                recorded_at=datetime.now(timezone.utc), source=source, fuzzy_key=None
            )
            recording = Recording(
                id=recording_id,
                request=masked_request,
                response=masked_response,
                metadata=metadata,
            )

            file_path.write_text(recording.model_dump_json(indent=2), encoding="utf-8")
            self._recordings[recording_id] = recording
            self._paths[recording_id] = file_path
            return recording

    async def delete(self, recording_id: str) -> bool:
        """Delete a recording by id (H4: id == filename stem). Returns False
        if `recording_id` is unknown (idempotent no-op, not an error)."""
        async with self._lock:
            if recording_id not in self._recordings:
                return False
            self._recordings.pop(recording_id, None)
            path = self._paths.pop(recording_id, None)
            if path is not None:
                path.unlink(missing_ok=True)
            return True

    def list(self) -> list[Recording]:
        """Return all currently known recordings (mutable-safe copy)."""
        return list(self._recordings.values())

    def snapshot(self) -> tuple[Recording, ...]:
        """Immutable snapshot for the matcher (D6) to iterate over without
        racing concurrent `save`/`delete` mutation."""
        return tuple(self._recordings.values())
