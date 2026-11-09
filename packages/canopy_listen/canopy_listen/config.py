"""Which models a recording uses: ~/.canopy-listen/config.json, set with `canopy-listen config`.

A meeting records the choice it was made with, so a transcript can be traced to its model.
"""
from __future__ import annotations

import json
import os

from .store import home

DEFAULTS = {
    "asr": "parakeet-v3",
    "speakers": "titanet",
    "final": "none",
    "language": "en",
    "same_voice": 0.66,
}


def path():
    return home() / "config.json"


def load() -> dict:
    try:
        saved = json.loads(path().read_text())
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def validate(key: str, value: str):
    from .engines import FINAL, SPEAKERS, SPEECH

    choices = {"asr": SPEECH, "speakers": SPEAKERS, "final": FINAL}
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting {key!r}; one of: {', '.join(DEFAULTS)}")
    if key in choices and value not in choices[key]:
        raise ValueError(f"{key} must be one of: {', '.join(choices[key])}")
    if key == "same_voice":
        number = float(value)
        if not 0.3 <= number <= 0.95:
            raise ValueError("same_voice must be between 0.3 and 0.95")
        return number
    return value


def save(changes: dict) -> dict:
    current = load()
    current.update({k: validate(k, v) for k, v in changes.items()})
    path().parent.mkdir(parents=True, exist_ok=True)
    tmp = path().with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2))
    os.replace(tmp, path())
    return current
