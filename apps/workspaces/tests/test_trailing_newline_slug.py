r"""Regression: a slug with a trailing newline must not survive the charset guard.

Python's `re` module special-cases `$`: unlike most regex engines, it matches
not only at the true end of a string but also immediately before a single
trailing `\n`. So the charset guard `^[a-z0-9][a-z0-9-]*$` — which reads as
"anchored start, anchored end" — actually accepts `"acme\n"`, because `$`
happily matches right before that trailing newline. Verified directly:

    >>> import re
    >>> bool(re.compile(r"^[a-z0-9][a-z0-9-]*$").search("acme\n"))
    True

Django's `RegexValidator` calls `.search()` (not `.fullmatch()`), so
`Workspace.full_clean()` / `Workspace.objects.create(slug="acme\n")` inherited
this hole even though `SLUG_PATTERN` "looks" fully anchored. The fix is to
anchor the model-level check with `\Z` (which Python's `re` treats as "only
the true end of string, no trailing-newline exception") rather than `$`.

Pydantic's `pattern=` (used by `WorkspaceCreateIn.slug` in schemas.py) is a
*different* regex engine — Pydantic v2 compiles `pattern=` with the Rust
`regex` crate by default, and Rust's `$` (without the multi-line flag) only
matches the true end of the haystack; it has no Python-style trailing-newline
exception. Verified empirically (see the docstring on
`test_schema_pattern_is_not_vulnerable_to_trailing_newline` below): a bare
`Field(pattern=SLUG_PATTERN)` already rejects `"acme\n"`. On top of that,
`WorkspaceCreateIn` inherits `StrictModel`'s `str_strip_whitespace=True`,
which strips the trailing newline *before* the pattern is even evaluated —
so the API layer normalizes `"acme\n"` down to the clean slug `"acme"`
instead of ever persisting the raw newline-bearing string. Both are
independently sufficient; neither required a code change. Rust's `regex`
crate also does not recognize `\Z` (only lowercase `\z`), so `SLUG_PATTERN`
itself must stay `$`-terminated — the model-level fix uses a
`\Z`-terminated pattern of its own rather than mutating the shared constant.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from pydantic import ValidationError as PydanticValidationError

from apps.workspaces.models import Workspace
from apps.workspaces.schemas import WorkspaceCreateIn

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(email="a@dimagi.com"):
    return User.objects.create(username=email, email=email)


# --- Schema layer (Pydantic `pattern=`) ---------------------------------


def test_schema_pattern_is_not_vulnerable_to_trailing_newline():
    """Pydantic's `pattern=` compiles to a Rust regex by default, and Rust's
    `$` (no multi-line flag) anchors to the TRUE end of the haystack — it has
    none of Python `re`'s "matches before a trailing \\n" behavior. A bare
    `Field(pattern=SLUG_PATTERN)` already rejects "acme\\n" on its own
    (verified with a standalone pydantic model with no whitespace-stripping
    config), so this schema was never vulnerable — it is documented here so
    the invariant doesn't silently regress if the regex engine or config
    changes.
    """
    from pydantic import BaseModel, Field

    from apps.workspaces.models import SLUG_PATTERN

    class _BareNoStrip(BaseModel):
        slug: str = Field(pattern=SLUG_PATTERN)

    with pytest.raises(PydanticValidationError):
        _BareNoStrip(slug="acme\n")


def test_schema_strips_trailing_whitespace_before_the_pattern_runs():
    """`WorkspaceCreateIn` inherits `StrictModel.str_strip_whitespace=True`,
    which normalizes the field BEFORE the pattern is checked — so
    `"acme\\n"` is silently cleaned to the valid slug `"acme"` rather than
    being rejected outright. This is a second, independent reason the schema
    layer was never exploitable: the raw newline-bearing string can never
    reach `Workspace.objects.create()` through this schema.
    """
    parsed = WorkspaceCreateIn(slug="acme\n", display_name="Acme")
    assert parsed.slug == "acme"


def test_schema_rejects_an_embedded_newline():
    """An embedded (non-trailing) newline is not whitespace-stripped away —
    `str.strip()` only trims the ends — so it must still fail the pattern."""
    with pytest.raises(PydanticValidationError):
        WorkspaceCreateIn(slug="ac\nme", display_name="Acme")


@pytest.mark.parametrize("slug", ["acme", "acme-eu", "a1"])
def test_schema_still_accepts_valid_slugs(slug):
    parsed = WorkspaceCreateIn(slug=slug, display_name="Acme")
    assert parsed.slug == slug


# --- Model layer (`full_clean()` / `RegexValidator`) --------------------


def test_model_full_clean_rejects_trailing_newline_slug():
    """`$` matches before a trailing newline in Python `re`, so
    `RegexValidator`'s `.search()`-based check let `"acme\\n"` through
    `Workspace.full_clean()`. The anchor must be `\\Z`, which has no such
    exception."""
    u = _user()
    ws = Workspace(slug="acme\n", display_name="Acme", created_by=u)
    with pytest.raises(ValidationError) as exc:
        ws.full_clean()
    assert "slug" in exc.value.error_dict


def test_model_full_clean_rejects_trailing_carriage_return_slug():
    u = _user()
    ws = Workspace(slug="acme\r", display_name="Acme", created_by=u)
    with pytest.raises(ValidationError) as exc:
        ws.full_clean()
    assert "slug" in exc.value.error_dict


def test_model_full_clean_rejects_embedded_newline_slug():
    u = _user()
    ws = Workspace(slug="ac\nme", display_name="Acme", created_by=u)
    with pytest.raises(ValidationError) as exc:
        ws.full_clean()
    assert "slug" in exc.value.error_dict


# --- save() path (direct ORM creation, bypassing the schema) ------------


def test_save_rejects_a_slug_with_a_trailing_newline():
    """The defect as filed: `Workspace.objects.create(slug="acme\\n")` must
    not succeed. `$` matches before a trailing newline in Python `re`, so the
    model-level guard needs `\\Z`, not `$`."""
    u = _user()
    with pytest.raises(ValidationError):
        Workspace.objects.create(slug="acme\n", display_name="Acme", created_by=u)
    assert not Workspace.objects.filter(slug="acme\n").exists()


def test_save_rejects_a_slug_with_a_trailing_carriage_return():
    u = _user()
    with pytest.raises(ValidationError):
        Workspace.objects.create(slug="acme\r", display_name="Acme", created_by=u)
    assert not Workspace.objects.filter(slug="acme\r").exists()


def test_save_rejects_a_slug_with_an_embedded_newline():
    u = _user()
    with pytest.raises(ValidationError):
        Workspace.objects.create(slug="ac\nme", display_name="Acme", created_by=u)
    assert not Workspace.objects.filter(slug="ac\nme").exists()


@pytest.mark.parametrize("slug", ["acme", "acme-eu", "a1"])
def test_save_still_accepts_valid_slugs(slug):
    u = _user()
    ws = Workspace.objects.create(slug=slug, display_name="Acme", created_by=u)
    assert Workspace.objects.filter(pk=ws.pk).exists()
