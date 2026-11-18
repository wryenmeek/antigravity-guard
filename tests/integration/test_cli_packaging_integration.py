"""Real subprocess packaging & plugin CLI integration tests."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _cli_exe(name: str) -> str:
    bin_dir = Path(sys.executable).parent
    candidate = bin_dir / name
    if candidate.is_file():
        return str(candidate)
    resolved = shutil.which(name)
    assert resolved is not None, f"Expected CLI executable '{name}' on PATH"
    return resolved


def test_cli_entrypoints_version_and_strict_plugin_validation() -> None:
    """Both antigravity-guard and antigravity-telemetry CLIs report v2.0.1 and validate plugin strictly."""
    for exe_name in ("antigravity-guard", "antigravity-telemetry"):
        proc = subprocess.run(
            [_cli_exe(exe_name), "version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
        assert "antigravity-guard v2.0.1" in proc.stdout

    val_proc = subprocess.run(
        [_cli_exe("antigravity-guard"), "validate-plugin", "--repo-root", str(REPO_ROOT), "--strict"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert val_proc.returncode == 0, val_proc.stdout + val_proc.stderr
