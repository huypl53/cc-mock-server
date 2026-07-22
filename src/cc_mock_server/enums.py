"""Shared enum constants reused across config, matcher, filter, and handler.

Kept as `str`-mixin Enums so they serialize as plain strings (JSON, YAML,
env vars) while still giving later phases typed constants instead of raw
string literals scattered through the codebase.
"""

from __future__ import annotations

from enum import Enum


class Mode(str, Enum):
    """Global proxy mode: serve from recordings vs. forward to the agent."""

    LIVE = "live"
    REPLAY = "replay"


class AgentMode(str, Enum):
    """Agent transport (D2): sync XOR pending, never mixed per request."""

    SYNC = "sync"
    PENDING = "pending"


class TimeoutFallback(str, Enum):
    """What to do when the agent times out or is otherwise unreachable."""

    RETURN_ERROR = "return_error"
    PASS_THROUGH = "pass_through"
    BUILT_IN = "built_in"


class ReplayMissStrategy(str, Enum):
    """What to do in replay mode when the fuzzy matcher finds no hit."""

    PASS_THROUGH = "pass_through"
    LIVE = "live"
    RETURN_ERROR = "return_error"


class FilterMode(str, Enum):
    """Domain filter semantics: whitelist-only vs. blacklist-exclude."""

    WHITELIST = "whitelist"
    BLACKLIST = "blacklist"
