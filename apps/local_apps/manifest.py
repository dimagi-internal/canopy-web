"""The local app manifest: what an app says it is, and the one validator for it.

Unlike a workspace app's manifest this describes real code, so nothing here can
be enforced against that code. `disclosure` is the author's statement for a
person and a reviewer to read; `canopy` is the only part with teeth, limiting
what the server will do for a run. The runner has its own looser validator
(`runner/canopy_runner/canopy_runner/local_apps/manifest.py`), and
`tests/test_local_apps_end_to_end.py` pins that whatever this approves, it accepts.
"""

from __future__ import annotations

import re

KEY = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")
VERSION = re.compile(r"^\d{1,6}(\.\d{1,6}){0,3}$")
PLATFORMS = ("macos",)
SETTING_TYPES = ("agent", "text", "longtext", "bool")
CANOPY_OPS = ("sessions.create", "sessions.send", "turns.status", "meetings.save")
TOP_LEVEL = {
    "key",
    "name",
    "description",
    "version",
    "changelog",
    "entry",
    "platforms",
    "source",
    "max_minutes",
    "notice",
    "disclosure",
    "settings",
    "checks",
    "canopy",
    "presentation",
}
MAX_MINUTES = 480
#: A step or state name, as an app's events and statuses carry it.
STATE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _fail(where: str, why: str):
    raise ValueError(f"{where}: {why}")


def _text(raw: dict, name: str, limit: int, *, required: bool = False) -> str:
    value = raw.get(name, "")
    if not isinstance(value, str) or len(value) > limit:
        _fail(name, f"must be text of at most {limit} characters")
    if required and not value.strip():
        _fail(name, "is required")
    return value.strip()


def _strings(where: str, value, *, limit: int = 300, max_items: int = 20) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > max_items:
        _fail(where, f"must be a list of at most {max_items}")
    if not all(isinstance(v, str) and v.strip() and len(v) <= limit for v in value):
        _fail(where, f"each must be non-empty text of at most {limit} characters")
    return [v.strip() for v in value]


def _settings(raw) -> dict:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict) or len(raw) > 30:
        _fail("settings", "must be an object of at most 30 settings")
    out = {}
    for name, spec in raw.items():
        if not re.match(r"^[a-z][a-z0-9_]{0,39}$", str(name)):
            _fail(f"settings.{name}", "must be an identifier")
        if not isinstance(spec, dict) or spec.get("type") not in SETTING_TYPES:
            _fail(f"settings.{name}.type", f"must be one of: {', '.join(SETTING_TYPES)}")
        unknown = set(spec) - {"type", "label", "help", "required", "default"}
        if unknown:
            _fail(f"settings.{name}", f"unknown keys: {', '.join(sorted(unknown))}")
        out[name] = {
            "type": spec["type"],
            "label": str(spec.get("label") or name)[:200],
            "help": str(spec.get("help") or "")[:500],
            "required": bool(spec.get("required", False)),
            # jsonb doesn't keep key order, and a form should follow the author's.
            "order": len(out),
        }
        if "default" in spec:
            out[name]["default"] = spec["default"]
    if sum(1 for s in out.values() if s["type"] == "agent") > 1:
        _fail("settings", "at most one setting may choose an agent")
    return out


def _checks(raw) -> list[dict]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > 10:
        _fail("checks", "must be a list of at most 10")
    out = []
    for i, check in enumerate(raw):
        if not isinstance(check, dict) or set(check) - {"label", "run", "fix"}:
            _fail(f"checks[{i}]", "must be {label, run, fix}")
        run = _strings(f"checks[{i}].run", check.get("run"), limit=200)
        if not run:
            _fail(f"checks[{i}].run", "must name a command")
        out.append(
            {
                "label": _text(check, "label", 200, required=True),
                "run": run,
                "fix": _text(check, "fix", 500, required=True),
            }
        )
    return out


def _presentation(raw) -> dict:
    """How the menu bar and the app page show the app: copy only, so not part of the trust
    surface. `steps` may say {agent}, filled with the agent the person chose."""
    if raw in (None, ""):
        return {"uses": "", "steps": [], "states": {}}
    if not isinstance(raw, dict) or set(raw) - {"uses", "steps", "states"}:
        _fail("presentation", "must be {uses, steps, states}")
    states = raw.get("states") or {}
    if not isinstance(states, dict) or len(states) > 40:
        _fail("presentation.states", "must be an object of at most 40 states")
    for name, text in states.items():
        if not STATE_NAME.match(str(name)):
            _fail(f"presentation.states.{name}", "must be a state name")
        if not isinstance(text, str) or not text.strip() or len(text) > 200:
            _fail(f"presentation.states.{name}", "must be non-empty text of at most 200 characters")
    uses = raw.get("uses", "")
    if not isinstance(uses, str) or len(uses) > 40:
        _fail("presentation.uses", "must be text of at most 40 characters")
    return {
        "uses": uses.strip(),
        "steps": _strings("presentation.steps", raw.get("steps"), limit=140, max_items=4),
        "states": {str(k): v.strip() for k, v in states.items()},
    }


def validate(raw: dict) -> dict:
    """The normalized manifest, or ValueError naming what is wrong."""
    if not isinstance(raw, dict):
        _fail("manifest", "must be an object")
    unknown = set(raw) - TOP_LEVEL
    if unknown:
        _fail("manifest", f"unknown keys: {', '.join(sorted(unknown))}")
    key = raw.get("key")
    if not isinstance(key, str) or not KEY.match(key):
        _fail("key", "must be lowercase letters, numbers and hyphens, 3 to 40 long")
    version = raw.get("version")
    if not isinstance(version, str) or not VERSION.match(version):
        _fail("version", "must be dotted numbers, such as 0.1.0")
    entry = _strings("entry", raw.get("entry"), limit=200)
    if not entry:
        _fail("entry", "must name what to run")
    if any(part == "{python}" for part in entry[1:]) or any(".." in part.split("/") for part in entry):
        _fail("entry", "{python} may only come first, and no part may climb out of the package")
    platforms = _strings("platforms", raw.get("platforms"), limit=20)
    if not platforms or set(platforms) - set(PLATFORMS):
        _fail("platforms", f"must list at least one of: {', '.join(PLATFORMS)}")
    source = _strings("source", raw.get("source"), limit=200)
    if "{python}" in source[1:] or any(".." in part.split("/") for part in source):
        _fail("source", "{python} may only come first, and no part may climb out of the package")
    minutes = raw.get("max_minutes")
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= MAX_MINUTES:
        _fail("max_minutes", f"must be a whole number from 1 to {MAX_MINUTES}")
    disclosure = raw.get("disclosure")
    if not isinstance(disclosure, dict) or set(disclosure) - {"reads", "sends"}:
        _fail("disclosure", "must be {reads, sends}")
    reads, sends = (
        _strings("disclosure.reads", disclosure.get("reads")),
        _strings("disclosure.sends", disclosure.get("sends")),
    )
    if not reads and not sends:
        _fail("disclosure", "must say what the app reads or sends")
    canopy = _strings("canopy", raw.get("canopy"), limit=40)
    if set(canopy) - set(CANOPY_OPS):
        _fail("canopy", f"must be among: {', '.join(CANOPY_OPS)}")
    settings = _settings(raw.get("settings"))
    if canopy and "sessions.create" in canopy and not any(s["type"] == "agent" for s in settings.values()):
        _fail("settings", "an app that opens sessions needs a setting that chooses the agent")
    return {
        "key": key,
        "name": _text(raw, "name", 120, required=True),
        "description": _text(raw, "description", 2000),
        "version": version,
        "changelog": _text(raw, "changelog", 4000),
        "entry": entry,
        "platforms": platforms,
        "source": source,
        "max_minutes": minutes,
        "notice": _text(raw, "notice", 1000, required=bool(source)),
        "disclosure": {"reads": reads, "sends": sends},
        "settings": settings,
        "checks": _checks(raw.get("checks")),
        "canopy": canopy,
        "presentation": _presentation(raw.get("presentation")),
    }
