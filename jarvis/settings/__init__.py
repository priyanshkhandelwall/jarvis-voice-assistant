"""Loads jarvis/settings/settings.toml into a single Settings object.

Every tunable number in the project (R17) is read from here, never
hard-coded in feature code. Set JARVIS_SETTINGS_PATH to point at a
different file, which the test suite uses to check overrides in isolation.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any


def _settings_path() -> Path:
    override = os.environ.get("JARVIS_SETTINGS_PATH")
    if override:
        return Path(override)
    return Path(__file__).with_name("settings.toml")


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """Read the TOML settings file and return it as a plain dict."""
    target = path or _settings_path()
    with open(target, "rb") as f:
        return tomllib.load(f)


SETTINGS = load_settings()


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_string_setting(section: str, key: str, value: str, path: Path | None = None) -> None:
    """Change one string setting in settings.toml, in place: only that one
    line is rewritten, so every comment and every other value stays as
    the developer wrote it. Also updates the loaded SETTINGS. Used by the
    dashboard's microphone picker ([audio] input_device_name)."""
    import re

    target = path or _settings_path()
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    in_section = False
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == f"[{section}]"
            continue
        if in_section and pattern.match(line):
            ending = "\r\n" if line.endswith("\r\n") else "\n"
            lines[i] = f"{key} = {_toml_string(value)}{ending}"
            break
    else:
        raise KeyError(f"[{section}] {key} not found in {target}")
    target.write_text("".join(lines), encoding="utf-8")
    if path is None:
        SETTINGS.setdefault(section, {})[key] = value
