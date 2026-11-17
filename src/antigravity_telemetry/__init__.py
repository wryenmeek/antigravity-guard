"""Backwards-compatible re-export shim for legacy `antigravity_telemetry` callers."""

from __future__ import annotations

import warnings

from antigravity_guard import __version__

warnings.warn(
    "antigravity_telemetry is deprecated; import antigravity_guard instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["__version__"]
