"""Shared pytest fixtures for cc_mock_server tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_yaml_config(tmp_path: Path):
    """Factory fixture: write a YAML config file and return its path."""

    def _write(content: str) -> Path:
        path = tmp_path / "cc-mock.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    return _write
