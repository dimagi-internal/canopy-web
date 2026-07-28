"""Workspace slug charset validation — a TENANCY invariant, not a cosmetic one.

A workspace slug is an addressing token: it is embedded in URLs, in Channels
group names, and — the reason this file exists — in the presence page key
`<app>:<workspace>:<resource>`. That key is parsed with a bounded
`split(":", 2)`, and the resource segment legitimately contains colons
(`opp:bednet/run-001`, `session:<id>`). So a slug containing a colon creates an
irreducible parsing ambiguity that no amount of care in the presence layer can
undo. The fix belongs HERE, where slugs are minted.

The charset is `^[a-z0-9][a-z0-9-]*$`, matching ace-web's `SLUG_RE` exactly so
the two sibling deployments agree on what a tenant may be called.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client

from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(email="a@dimagi.com"):
    return User.objects.create(username=email, email=email)


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _post(c, url, data):
    return c.post(url, data=json.dumps(data), content_type="application/json")


# --- API layer ---------------------------------------------------------


def test_api_rejects_a_colon_bearing_slug():
    """The headline case: `acme:eu` is what makes `canopy:acme:eu:activity`
    ambiguous with `canopy:acme` + resource `eu:activity`."""
    u = _user()
    r = _post(_client(u), "/api/workspaces/", {"slug": "acme:eu", "display_name": "Acme EU"})
    assert r.status_code == 422, r.content
    assert not Workspace.objects.filter(slug="acme:eu").exists()


@pytest.mark.parametrize(
    "slug",
    [
        "Acme",         # uppercase
        "-acme",        # leading hyphen
        "acme.eu",      # dot
        "has spaces",   # whitespace
        "acmé",         # unicode
        "acme_eu",      # underscore — outside ace-web's charset
        "~global",      # the presence global sentinel
        "acme/eu",      # path separator
    ],
)
def test_api_rejects_out_of_charset_slugs(slug):
    u = _user()
    r = _post(_client(u), "/api/workspaces/", {"slug": slug, "display_name": "X"})
    assert r.status_code == 422, r.content
    assert not Workspace.objects.filter(slug=slug).exists()


@pytest.mark.parametrize("slug", ["acme", "acme-eu", "a1", "9", "a" * 64])
def test_api_still_accepts_valid_slugs(slug):
    u = _user()
    r = _post(_client(u), "/api/workspaces/", {"slug": slug, "display_name": "X"})
    assert r.status_code == 201, r.content
    assert Workspace.objects.filter(slug=slug).exists()


def test_api_keeps_the_existing_length_bounds():
    u = _user()
    assert _post(_client(u), "/api/workspaces/",
                 {"slug": "", "display_name": "X"}).status_code == 422
    assert _post(_client(u), "/api/workspaces/",
                 {"slug": "a" * 65, "display_name": "X"}).status_code == 422


# --- Model layer -------------------------------------------------------


def test_model_rejects_a_colon_slug_created_directly():
    """A schema only guards the API path. Shells, management commands and
    ad-hoc scripts go straight to the ORM, so the invariant is enforced on
    the model too."""
    u = _user()
    with pytest.raises(ValidationError):
        Workspace.objects.create(slug="acme:eu", display_name="Acme EU", created_by=u)
    assert not Workspace.objects.filter(slug="acme:eu").exists()


def test_model_full_clean_rejects_out_of_charset_slugs():
    u = _user()
    ws = Workspace(slug="Acme EU", display_name="Acme EU", created_by=u)
    with pytest.raises(ValidationError) as exc:
        ws.full_clean()
    assert "slug" in exc.value.error_dict


def test_model_accepts_a_valid_slug():
    u = _user()
    ws = Workspace.objects.create(slug="acme-eu", display_name="Acme EU", created_by=u)
    assert Workspace.objects.filter(slug="acme-eu").exists()
    # re-saving a conforming row is unaffected by the validation on the save path
    ws.display_name = "Acme Europe"
    ws.save()
    assert Workspace.objects.get(slug="acme-eu").display_name == "Acme Europe"


# --- Regression --------------------------------------------------------


def test_a_slug_that_would_split_a_presence_page_key_cannot_be_created():
    """REGRESSION — cross-tenant presence leak via a colon-bearing slug.

    Presence rosters are keyed by a client-supplied
    `<app>:<workspace>:<resource>` string parsed with `split(":", 2)`. The
    resource segment legitimately contains colons (`opp:bednet/run-001`), so
    the workspace segment can never be disambiguated from a slug that
    contains one.

    Concretely: a user who is a member of BOTH `acme` and `acme:eu`, viewing
    `/w/acme:eu/activity`, emits `canopy:acme:eu:activity`. That parses as
    workspace `acme`, resource `eu:activity` — the membership gate passes
    (they really are an `acme` member) and they are written into `acme`'s
    roster, where an `acme`-only member reads their name, email and
    sub-location and learns about activity in a workspace they cannot see.

    The presence layer cannot fix this: both readings are legitimate. So the
    fix is that `acme:eu` is not a nameable tenant, by any route.
    """
    u = _user()
    payload = {"slug": "acme:eu", "display_name": "Acme EU"}
    assert _post(_client(u), "/api/workspaces/", payload).status_code == 422
    with pytest.raises(ValidationError):
        Workspace.objects.create(slug="acme:eu", display_name="Acme EU", created_by=u)
    assert not Workspace.objects.filter(slug__contains=":").exists()
