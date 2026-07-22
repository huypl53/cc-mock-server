"""Runtime configuration for cc-mock-server.

`Config` is the single source of truth for every field the other phases
depend on (proxy/control ports, mode, agent transport, timeouts, replay
miss strategy, matcher confidence gate, pending cap, domain filter,
recordings location — see plan.md Cross-Cutting Decisions D1-D9).

Precedence when loading: CLI args > environment (`CC_MOCK_*`) > YAML file
> field defaults (highest wins, later layers overwrite earlier ones).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from cc_mock_server.enums import AgentMode, FilterMode, Mode, ReplayMissStrategy, TimeoutFallback

#: Hostnames treated as loopback for the purposes of `is_loopback()`.
#: `agent_url` must resolve to one of these (D3) unless a phase-7 flag
#: explicitly opts out of the loopback-only guard.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: Prefix for environment-variable overrides, e.g. `CC_MOCK_PROXY_PORT`.
ENV_PREFIX = "CC_MOCK_"

#: Config fields whose env-var value is a comma-separated list rather than
#: a scalar. Extend this set if future list-typed fields are added.
_LIST_FIELDS = frozenset({"filter_domains"})


def is_loopback(url: str) -> bool:
    """Return True if `url`'s host is a loopback address.

    Accepts either a full URL (`http://127.0.0.1:8080/path`) or a bare
    host[:port] string (`127.0.0.1:8080`, `localhost`). Used to guard
    `agent_url` against pointing at a non-loopback host (D3).
    """
    if not url:
        return False
    # urlparse needs a scheme (or `//`) to correctly split host from path
    # for bare "host:port" inputs; without it, "host:port" is parsed as
    # scheme="host", path="port".
    candidate = url if "//" in url else f"//{url}"
    host = urlparse(candidate).hostname
    if host is None:
        return False
    return host.lower() in LOOPBACK_HOSTS


class Config(BaseModel):
    """Validated application configuration.

    See plan.md Cross-Cutting Decisions for the rationale behind each
    field's default and the validators enforced below.
    """

    proxy_port: int = 8080
    control_port: int = 8081
    control_bind: str = "127.0.0.1"
    mode: Mode = Mode.LIVE
    agent_url: Optional[str] = None
    agent_mode: AgentMode = AgentMode.PENDING
    agent_timeout: float = 10.0
    timeout_fallback: TimeoutFallback = TimeoutFallback.RETURN_ERROR
    replay_miss_strategy: ReplayMissStrategy = ReplayMissStrategy.PASS_THROUGH
    min_confidence: float = 0.6
    max_pending: int = 100
    filter_mode: FilterMode = FilterMode.WHITELIST
    filter_domains: list[str] = Field(default_factory=list)
    recordings_dir: Path = Path("recordings")

    @field_validator("agent_timeout")
    @classmethod
    def _agent_timeout_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("agent_timeout must be > 0")
        return value

    @field_validator("min_confidence")
    @classmethod
    def _min_confidence_in_unit_interval(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("min_confidence must be within [0, 1]")
        return value

    @field_validator("max_pending")
    @classmethod
    def _max_pending_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("max_pending must be > 0")
        return value

    @field_validator("filter_domains")
    @classmethod
    def _normalize_filter_domains(cls, value: list[str]) -> list[str]:
        return [domain.lower() for domain in value]

    @model_validator(mode="after")
    def _sync_agent_mode_requires_agent_url(self) -> "Config":
        if self.agent_mode == AgentMode.SYNC and not self.agent_url:
            raise ValueError(
                "agent_url is required when agent_mode is 'sync' "
                "(pending mode allows agent_url to be unset)"
            )
        return self


def _load_yaml_layer(path: Path) -> dict[str, Any]:
    """Read a YAML config file into a plain dict. Missing file → `{}`."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML config at {path} must be a mapping, got {type(data).__name__}")
    return data


def _env_layer(env: Mapping[str, str]) -> dict[str, Any]:
    """Extract `CC_MOCK_*` entries from `env` into a field-name-keyed dict."""
    overrides: dict[str, Any] = {}
    for key, raw_value in env.items():
        if not key.startswith(ENV_PREFIX):
            continue
        field_name = key[len(ENV_PREFIX) :].lower()
        if field_name in _LIST_FIELDS:
            overrides[field_name] = [
                item.strip() for item in raw_value.split(",") if item.strip()
            ]
        else:
            overrides[field_name] = raw_value
    return overrides


def _cli_layer(cli_overrides: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Drop `None` values so unset CLI flags don't clobber lower layers."""
    if not cli_overrides:
        return {}
    return {key: value for key, value in cli_overrides.items() if value is not None}


def load_config(
    cli_overrides: Optional[Mapping[str, Any]] = None,
    yaml_path: Optional[Path | str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Config:
    """Build a `Config`, merging layers with precedence CLI > env > YAML > default.

    Args:
        cli_overrides: mapping of field name -> value from parsed CLI args.
            Entries whose value is `None` are treated as "not provided" and
            do not override lower-precedence layers.
        yaml_path: path to an optional YAML config file. A missing path is
            not an error (treated as an empty layer).
        env: environment mapping to scan for `CC_MOCK_*` keys. Defaults to
            `os.environ` when not provided.

    Returns:
        A fully validated `Config` instance.

    Raises:
        pydantic.ValidationError: if the merged layers fail validation.
    """
    merged: dict[str, Any] = {}

    if yaml_path is not None:
        merged.update(_load_yaml_layer(Path(yaml_path)))

    merged.update(_env_layer(env if env is not None else os.environ))
    merged.update(_cli_layer(cli_overrides))

    return Config(**merged)
