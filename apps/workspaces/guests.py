"""Guests: people in a workspace for ONE thing it runs, and outside it for everything else.

A guest is a `WorkspaceMembership` with role `guest` and a `scope` naming what it opens,
`{"apps": {"<app key>": "<app role>"}}`. The relationship is explicit (listed, invited and
removed like any member), but every reader in `services` answers None for a guest unless its
caller opts in (`include_guests=True`), so the workspace's gates, access-list lines, mail
routes and listings never see one. Only code written for guests opts in.

What a scope may name is the business of the app that owns it, which this framework app
cannot import (ARCHITECTURE.md § Layers): the product app registers a validator from its
`AppConfig.ready()` (`register`), and `validate_scope` refuses any scope no validator vouches
for — no validator, no guests. See docs/architecture/access.md § Guests.
"""
from __future__ import annotations

from collections.abc import Callable

#: Per scope kind ("apps"), the validator that vouches for it:
#: `fn(workspace_slug, entries: dict, by) -> dict` returns the normalised entries or raises
#: `ScopeError`. `by` is the person granting.
_VALIDATORS: dict[str, Callable] = {}
#: Per scope kind, how its entries read on the members list: `fn(workspace_slug, entries) -> [str]`.
_DESCRIBERS: dict[str, Callable] = {}


class ScopeError(ValueError):
    """A guest scope that names nothing or something not there (422), or something `by`
    may not give (403)."""

    def __init__(self, message: str, *, status: int = 422):
        self.status = status
        super().__init__(message)


def register(kind: str, validate: Callable, describe: Callable | None = None) -> None:
    """Register the validator (and the describer) for one scope kind. Registering again
    replaces it."""
    _VALIDATORS[kind] = validate
    if describe is not None:
        _DESCRIBERS[kind] = describe


def describe(workspace: str, scope: dict | None) -> str:
    """A guest's line on the members list: "Guest: Household (assistant)"."""
    parts: list[str] = []
    for kind, entries in (scope or {}).items():
        fn = _DESCRIBERS.get(kind)
        parts += fn(workspace, entries) if fn else [f"{k} ({v})" for k, v in (entries or {}).items()]
    return "Guest: " + ", ".join(parts) if parts else "Guest"


def validate_scope(workspace: str, scope, *, by=None) -> dict:
    """The normalised scope for a guest of `workspace`, or `ScopeError`.

    Non-empty, only registered kinds, and each kind's entries vouched for by its validator
    (for apps: installed here, a role the app declares, and `by` may run that app)."""
    if not isinstance(scope, dict) or not scope:
        raise ScopeError("a guest needs a scope, e.g. {\"apps\": {\"<app>\": \"<role>\"}}")
    out: dict = {}
    for kind, entries in scope.items():
        fn = _VALIDATORS.get(kind)
        if fn is None:
            raise ScopeError(f"a guest scope cannot name {kind!r}")
        if not isinstance(entries, dict) or not entries:
            raise ScopeError(f"a guest's {kind!r} scope names nothing")
        out[kind] = fn(workspace, entries, by)
    return out
