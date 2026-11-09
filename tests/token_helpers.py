"""Bearer credentials for tests: mint a personal token and call the API with it.

One home for the `_pat` / `_bearer` helpers test modules share. Pick the
shape the call site needs:

    pat_token(user)            -> the raw token string
    bearer_headers(token)      -> {"HTTP_AUTHORIZATION": "Bearer <token>"}, for `client.get(url, **headers)`
    bearer_client(token)       -> a Client that sends that header on every request
    pat_headers(user)          -> mint, then `bearer_headers`
    pat_client(user, **extra)  -> mint, then `bearer_client` (extra kwargs become request defaults)
    delegated_headers(app, user, bound=None) -> a host-minted embed token as headers
"""
from __future__ import annotations

from django.test import Client

from apps.tokens.models import DelegatedToken, PersonalToken


def pat_token(user, label: str = "test") -> str:
    """A fresh personal access token for `user`; returns the raw secret."""
    raw, _row = PersonalToken.create_for_user(user=user, label=label)
    return raw


def bearer_headers(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def bearer_client(token: str, **extra) -> Client:
    return Client(**bearer_headers(token), **extra)


def pat_headers(user, label: str = "test") -> dict:
    return bearer_headers(pat_token(user, label))


def pat_client(user, label: str = "test", **extra) -> Client:
    return bearer_client(pat_token(user, label), **extra)


def delegated_headers(app, user, bound=None, ttl_seconds: int = 3600) -> dict:
    """Headers carrying an embed token the host `app` minted for `user` (optionally bound to an agent)."""
    kwargs = {"bound_agent": bound} if bound is not None else {}
    raw, _row = DelegatedToken.issue(app=app, user=user, ttl_seconds=ttl_seconds, **kwargs)
    return bearer_headers(raw)
