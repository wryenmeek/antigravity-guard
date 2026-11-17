"""Plugin bundle strict validation and local Antigravity sync helpers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from antigravity_guard import __version__


def _read_pyproject_version(repo_root: Path) -> str | None:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    return str(version) if isinstance(version, str) else None


def _check_required_plugin_assets(plugin_dir: Path, manifest: dict[str, Any]) -> list[str]:
    """Validates hooks, logo SVG, skills, and rules inside the plugin bundle."""
    errors: list[str] = []
    logo_rel = manifest.get("logo")
    if not isinstance(logo_rel, str) or not (plugin_dir / logo_rel).is_file():
        errors.append("Missing valid `logo` asset file referenced in plugin/plugin.json")

    hooks_file = plugin_dir / "hooks.json"
    if not hooks_file.is_file():
        errors.append("Missing plugin/hooks.json")
    else:
        try:
            json.loads(hooks_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid JSON in plugin/hooks.json: {exc}")

    if not any((plugin_dir / "skills").glob("*/SKILL.md")):
        errors.append("Missing plugin/skills/*/SKILL.md")
    if not (plugin_dir / "rules" / "AGENTS.md").is_file():
        errors.append("Missing plugin/rules/AGENTS.md")
    return errors


def _run_agy_strict_cli(plugin_dir: Path) -> list[str]:
    """Invokes `agy plugin validate --strict --json` when `agy` CLI is available locally."""
    agy_bin = shutil.which("agy") or "/Users/wryen/.local/bin/agy"
    if not Path(agy_bin).is_file():
        return []
    proc = subprocess.run(
        [agy_bin, "plugin", "validate", "--strict", "--json", str(plugin_dir)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stdout.strip() or proc.stderr.strip()
        return [f"agy plugin validate --strict failed: {detail}"]
    return []


def validate_plugin_bundle(repo_root: Path | None = None, strict: bool = True) -> tuple[bool, list[str]]:
    """Validates plugin manifest metadata, version parity, assets, and optional `agy` strict check."""
    root = (repo_root or Path(__file__).resolve().parents[2]).resolve()
    plugin_dir = root / "plugin"
    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.is_file():
        return False, [f"Missing plugin manifest: {manifest_path}"]

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if manifest.get("name") != "antigravity-guard":
        errors.append(f"Expected plugin name 'antigravity-guard', got {manifest.get('name')!r}")
    if manifest.get("version") != __version__:
        errors.append(f"plugin.json version {manifest.get('version')!r} != __version__ {__version__!r}")
    py_ver = _read_pyproject_version(root)
    if py_ver is not None and py_ver != __version__:
        errors.append(f"pyproject.toml version {py_ver!r} != __version__ {__version__!r}")
    if strict:
        if not manifest.get("displayName"):
            errors.append("Missing `displayName` in plugin/plugin.json")
        if not manifest.get("suggestedPrompts"):
            errors.append("Missing `suggestedPrompts` in plugin/plugin.json")
        errors.extend(_check_required_plugin_assets(plugin_dir, manifest))
        if not errors:
            errors.extend(_run_agy_strict_cli(plugin_dir))

    return not errors, errors


def _ensure_symlink(target: Path, link_path: Path) -> None:
    """Creates or refreshes a symlink safely when target exists."""
    if not target.exists():
        return
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.is_symlink() or link_path.exists():
        link_path.unlink()
    link_path.symlink_to(target)


def sync_plugin_local(
    repo_root: Path | None = None,
    target_config_dir: Path | None = None,
    clean_legacy: bool = True,
) -> dict[str, Any]:
    """Synchronizes `plugin/` to `~/.gemini/config/plugins/antigravity-guard/` and removes legacy copy."""
    root = (repo_root or Path(__file__).resolve().parents[2]).resolve()
    ok, errors = validate_plugin_bundle(repo_root=root, strict=True)
    if not ok:
        raise RuntimeError("Plugin validation failed:\n" + "\n".join(f"  - {e}" for e in errors))

    plugins_root = (
        target_config_dir.parent
        if target_config_dir is not None
        else Path(os.path.expanduser("~/.gemini/config/plugins"))
    )
    dest_dir = target_config_dir or (plugins_root / "antigravity-guard")
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    shutil.copytree(root / "plugin", dest_dir)
    (dest_dir / "installed_version.json").write_text(
        json.dumps({"version": __version__}, indent=2) + "\n",
        encoding="utf-8",
    )

    legacy_dir = plugins_root / "antigravity-telemetry-guard"
    removed_legacy = False
    if clean_legacy and legacy_dir.exists() and legacy_dir.resolve() != dest_dir.resolve():
        shutil.rmtree(legacy_dir)
        removed_legacy = True

    if target_config_dir is None:
        local_bin = Path(os.path.expanduser("~/.local/bin"))
        agy_bin = Path(os.path.expanduser("~/.gemini/antigravity/bin"))
        for exe in ("antigravity-guard", "antigravity-telemetry"):
            _ensure_symlink(local_bin / exe, agy_bin / exe)

    return {"installed_dir": str(dest_dir), "version": __version__, "removed_legacy": removed_legacy}
