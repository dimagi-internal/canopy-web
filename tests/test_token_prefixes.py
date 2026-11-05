"""New personal, delegated and contact tokens carry a prefix (`cpat_`, `cdt_`, `ccon_`).

The prefix says what a token is before anything is looked up: `resolve_bearer`
does one lookup for a prefixed token, a secret scanner and the transcript
scrubber can recognise one, and a person can tell a leaked value's kind. Tokens
minted before the prefix (43 bare base64url characters) keep working through the
old multi-lookup.
"""
from __future__ import annotations

import secrets

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.common.token_hash import hash_token
from apps.contacts.models import Contact
from apps.tokens import principal as P
from apps.tokens.models import AppCredential, ContactToken, DelegatedToken, PersonalToken
from tests.site_tenant import host_workspace

pytestmark = pytest.mark.django_db

TABLES = ("personal_tokens", "delegated_tokens", "contact_tokens")


@pytest.fixture()
def user():
    return User.objects.create_user("u", "u@dimagi.com", "pw")


@pytest.fixture()
def app(user):
    return AppCredential.create_credential(name="a", created_by=user, workspace=host_workspace())


def _contact(app):
    return Contact.objects.create(workspace=app.workspace, email="v@example.org")


def _token_lookups(captured) -> list[str]:
    return [t for q in captured for t in TABLES if f'"{t}"' in q["sql"] or f" {t} " in q["sql"]]


def test_new_tokens_carry_their_kinds_prefix(user, app):
    pat, _ = PersonalToken.create_for_user(user=user, label="x")
    dt, _ = DelegatedToken.issue(app=app, user=user, ttl_seconds=60)
    ct, _ = ContactToken.issue(app=app, contact=_contact(app), ttl_seconds=60)
    assert pat.startswith("cpat_") and dt.startswith("cdt_") and ct.startswith("ccon_")
    assert len({PersonalToken.PREFIX, DelegatedToken.PREFIX, ContactToken.PREFIX,
                "cct_", "crt_", "chk_", "hcpat_"}) == 7


@pytest.mark.parametrize("kind", ["pat", "delegated", "contact"])
def test_a_prefixed_token_resolves_with_one_lookup(user, app, kind):
    raw = {
        "pat": lambda: PersonalToken.create_for_user(user=user, label="x")[0],
        "delegated": lambda: DelegatedToken.issue(app=app, user=user, ttl_seconds=60)[0],
        "contact": lambda: ContactToken.issue(app=app, contact=_contact(app), ttl_seconds=60)[0],
    }[kind]()
    with CaptureQueriesContext(connection) as q:
        found = P.resolve_bearer(raw)
    assert found is not None and found.kind == kind
    assert len(_token_lookups(q.captured_queries)) == 1, [x["sql"] for x in q.captured_queries]


def _legacy(model, **fields):
    """A token as minted before prefixes: 43 bare base64url characters."""
    raw = secrets.token_urlsafe(32)
    model.objects.create(token_hash=hash_token(raw), **fields)
    return raw


def test_a_legacy_unprefixed_pat_still_authenticates(user, client):
    from django.test import Client

    raw = _legacy(PersonalToken, user=user, label="old")
    assert P.resolve_bearer(raw).kind == P.PAT
    assert Client().get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {raw}").status_code == 200


def test_a_legacy_unprefixed_delegated_token_still_resolves(user, app):
    from django.utils import timezone

    raw = _legacy(DelegatedToken, app=app, user=user,
                  expires_at=timezone.now() + timezone.timedelta(minutes=5))
    assert P.resolve_bearer(raw).kind == P.DELEGATED


def test_a_legacy_token_that_happens_to_start_like_a_prefix_still_resolves(user):
    """A bare 43-character token can begin `cpat_` by chance; length tells it apart."""
    raw = "cdt_" + secrets.token_urlsafe(30)[:39]
    assert len(raw) == 43
    PersonalToken.objects.create(user=user, label="old", token_hash=hash_token(raw))
    assert P.resolve_bearer(raw).kind == P.PAT


def test_a_token_under_another_kinds_prefix_is_refused(user, app):
    pat, _ = PersonalToken.create_for_user(user=user, label="x")
    body = pat[len(PersonalToken.PREFIX):]
    assert P.resolve_bearer(DelegatedToken.PREFIX + body) is None
    assert P.resolve_bearer(ContactToken.PREFIX + body) is None
    dt, _ = DelegatedToken.issue(app=app, user=user, ttl_seconds=60)
    assert P.resolve_bearer(PersonalToken.PREFIX + dt[len(DelegatedToken.PREFIX):]) is None


def test_a_prefixed_token_a_door_does_not_take_is_refused_without_a_lookup(user, app):
    dt, _ = DelegatedToken.issue(app=app, user=user, ttl_seconds=60)
    with CaptureQueriesContext(connection) as q:
        assert P.resolve_bearer(dt, accept={P.PAT}) is None
    assert _token_lookups(q.captured_queries) == []


def test_the_transcript_scrubber_recognises_canopy_tokens(user):
    from apps.session_sharing.redact import redact_text

    pat, _ = PersonalToken.create_for_user(user=user, label="x")
    text, n = redact_text(f"the token is {pat} and done")
    assert pat not in text and n == 1
