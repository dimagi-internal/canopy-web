"""WS auth — delegated Bearer + `?token=` query param (SP4 PR 2, Task 4).

Extends RealtimeAuthMiddleware's bearer resolver to fall back to DelegatedToken
after a PersonalToken miss, and adds a query-string `?token=` resolver for
browsers that can't set an Authorization header on `new WebSocket()`. The query
resolver accepts DelegatedTokens ONLY — a long-lived PAT in a URL is rejected
by design (URLs end up in access logs, browser history, referrers).
"""
from __future__ import annotations

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User

from apps.realtime import channels_auth
from apps.tokens.models import AppCredential, DelegatedToken, PersonalToken

pytestmark = pytest.mark.django_db


@pytest.fixture()
def delegated():
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    _, cred = AppCredential.create_credential(name="a", domains=["dimagi.com"], created_by=user)
    raw, _ = DelegatedToken.issue(app=cred, user=user, ttl_seconds=600)
    return user, raw


def _scope(query=b"", headers=()):
    return {"query_string": query, "headers": list(headers)}


def test_bearer_resolves_delegated_token(delegated):
    user, raw = delegated
    scope = _scope(headers=[(b"authorization", f"Bearer {raw}".encode())])
    assert async_to_sync(channels_auth._user_from_bearer)(scope).pk == user.pk


def test_query_token_resolves_delegated_only(delegated):
    user, raw = delegated
    assert async_to_sync(channels_auth._user_from_query_token)(
        _scope(query=f"token={raw}".encode())).pk == user.pk
    # a PAT on the query string must NOT authenticate
    raw_pat, _ = PersonalToken.create_for_user(user=user, label="x")
    assert async_to_sync(channels_auth._user_from_query_token)(
        _scope(query=f"token={raw_pat}".encode())) is None


def test_bearer_rejects_delegated_token_for_deactivated_user(delegated):
    """F1: a delegated token minted before deactivation must stop authenticating
    over WS once the user is deactivated."""
    user, raw = delegated
    user.is_active = False
    user.save(update_fields=["is_active"])
    scope = _scope(headers=[(b"authorization", f"Bearer {raw}".encode())])
    assert async_to_sync(channels_auth._user_from_bearer)(scope) is None


def test_query_token_rejects_delegated_token_for_deactivated_user(delegated):
    user, raw = delegated
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert async_to_sync(channels_auth._user_from_query_token)(
        _scope(query=f"token={raw}".encode())) is None
