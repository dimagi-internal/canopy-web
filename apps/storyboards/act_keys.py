"""Deriving stable act keys — the one place, so the two write paths agree.

``import_storyboard`` and ``PATCH /api/storyboards/{slug}`` both replace acts
wholesale, and both must produce the SAME keys from the same arc, or pushing the
file after an in-app edit would silently re-point every act note.
"""
from __future__ import annotations

from django.utils.text import slugify

_MAX = 120


def act_key(declared: str, title: str, position: int, taken: set[str]) -> str:
    """A key for one act, unique within ``taken`` (which this mutates).

    An explicit ``key:`` wins — that is how an author keeps existing notes
    attached through a retitle. Otherwise the title, which is stable across a
    re-import that did not change it. A titleless act falls back to its
    position, which is the honest answer: there is nothing else to identify it
    by.
    """
    base = slugify(declared or title)[:_MAX] or f"act-{position + 1}"
    key = base
    n = 2
    while key in taken:
        # Two acts with the same title is a real (if odd) arc. Deterministic
        # given the same order, so a re-import reproduces the same assignment.
        suffix = f"-{n}"
        key = f"{base[: _MAX - len(suffix)]}{suffix}"
        n += 1
    taken.add(key)
    return key
