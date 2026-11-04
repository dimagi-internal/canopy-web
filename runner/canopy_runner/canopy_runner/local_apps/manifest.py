"""Validate a local app's manifest.json.

Self-contained and stdlib-only: the server keeps its own copy, and a test pins the two
equal, because a package the server approves must be one every runner accepts.
"""
from __future__ import annotations

import re

KEY_RX = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")
VERSION_RX = re.compile(r"^\d{1,9}(\.\d{1,9}){0,3}$")
SETTING_NAME_RX = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
PYTHON = "{python}"
PLATFORMS = {"macos"}
SETTING_TYPES = {"agent", "text", "longtext", "bool"}
CANOPY_OPS = {"sessions.create", "sessions.send", "turns.status"}
SHORT = 40
MAX_MINUTES = 480

REQUIRED = ("key", "name", "version", "entry", "platforms", "max_minutes")
KNOWN = {*REQUIRED, "description", "changelog", "consumes", "provides", "notice",
         "disclosure", "settings", "checks", "canopy"}


def _text(raw: dict, name: str) -> str:
    value = raw.get(name, "")
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _strings(value, name: str) -> list:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{name} must be a list of non-empty strings")
    return list(value)


def _short_strings(raw: dict, name: str) -> list:
    values = _strings(raw.get(name, []), name)
    if any(len(v) > SHORT for v in values):
        raise ValueError(f"{name} entries must be at most {SHORT} characters")
    return values


def _argv(value, name: str) -> list:
    """A command: `{python}` stands for the runner's interpreter, and only as the program."""
    argv = _strings(value, name)
    if not argv:
        raise ValueError(f"{name} must not be empty")
    if PYTHON in argv[1:]:
        raise ValueError(f"{name}: {PYTHON} may only come first")
    return argv


def _disclosure(raw: dict) -> dict:
    value = raw.get("disclosure", {})
    if not isinstance(value, dict):
        raise ValueError("disclosure must be an object")
    unknown = set(value) - {"reads", "sends"}
    if unknown:
        raise ValueError(f"disclosure has unknown keys: {sorted(unknown)}")
    return {"reads": _strings(value.get("reads", []), "disclosure.reads"),
            "sends": _strings(value.get("sends", []), "disclosure.sends")}


def _settings(raw: dict) -> dict:
    value = raw.get("settings", {})
    if not isinstance(value, dict):
        raise ValueError("settings must be an object")
    out = {}
    for name, spec in value.items():
        if not SETTING_NAME_RX.match(str(name)):
            raise ValueError(f"setting name {name!r} is not valid")
        if not isinstance(spec, dict):
            raise ValueError(f"setting {name} must be an object")
        if spec.get("type") not in SETTING_TYPES:
            raise ValueError(f"setting {name}: type must be one of {sorted(SETTING_TYPES)}")
        if not isinstance(spec.get("label", ""), str):
            raise ValueError(f"setting {name}: label must be a string")
        if not isinstance(spec.get("required", False), bool):
            raise ValueError(f"setting {name}: required must be true or false")
        out[name] = dict(spec)
    return out


def _checks(raw: dict) -> list:
    value = raw.get("checks", [])
    if not isinstance(value, list):
        raise ValueError("checks must be a list")
    out = []
    for i, check in enumerate(value):
        if not isinstance(check, dict):
            raise ValueError(f"checks[{i}] must be an object")
        for name in ("label", "fix"):
            if not isinstance(check.get(name), str) or not check[name]:
                raise ValueError(f"checks[{i}].{name} must be a non-empty string")
        out.append({"label": check["label"], "run": _argv(check.get("run"), f"checks[{i}].run"),
                    "fix": check["fix"]})
    return out


def validate(raw: dict) -> dict:
    """The manifest with every optional field filled in, or ValueError naming the fault."""
    if not isinstance(raw, dict):
        raise ValueError("a manifest must be an object")
    unknown = set(raw) - KNOWN
    if unknown:
        raise ValueError(f"unknown manifest keys: {sorted(unknown)}")
    missing = [name for name in REQUIRED if name not in raw]
    if missing:
        raise ValueError(f"manifest is missing: {missing}")

    key = raw["key"]
    if not isinstance(key, str) or not KEY_RX.match(key):
        raise ValueError(f"key {key!r} is not valid")
    version = raw["version"]
    if not isinstance(version, str) or not VERSION_RX.match(version):
        raise ValueError(f"version {version!r} must be dotted numbers")
    name = _text(raw, "name")
    if not name:
        raise ValueError("name must not be empty")

    platforms = _strings(raw["platforms"], "platforms")
    if not set(platforms) <= PLATFORMS:
        raise ValueError(f"platforms must be a subset of {sorted(PLATFORMS)}")
    max_minutes = raw["max_minutes"]
    if isinstance(max_minutes, bool) or not isinstance(max_minutes, int) \
            or not 1 <= max_minutes <= MAX_MINUTES:
        raise ValueError(f"max_minutes must be a whole number from 1 to {MAX_MINUTES}")
    canopy = _strings(raw.get("canopy", []), "canopy")
    if not set(canopy) <= CANOPY_OPS:
        raise ValueError(f"canopy must be a subset of {sorted(CANOPY_OPS)}")

    return {
        "key": key,
        "name": name,
        "description": _text(raw, "description"),
        "version": version,
        "changelog": _text(raw, "changelog"),
        "entry": _argv(raw["entry"], "entry"),
        "platforms": platforms,
        "consumes": _short_strings(raw, "consumes"),
        "provides": _short_strings(raw, "provides"),
        "max_minutes": max_minutes,
        "notice": _text(raw, "notice"),
        "disclosure": _disclosure(raw),
        "settings": _settings(raw),
        "checks": _checks(raw),
        "canopy": canopy,
    }
