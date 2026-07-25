"""Pre-auth invite preview/accept routes get a narrow login-middleware carve-out.

`/api/workspaces/invites/{token}/preview` (auth=None) and
`/api/workspaces/invites/{token}/accept` (session_auth, self-enforcing) share a
literal "invites" path segment with the OWNER-ONLY invite CRUD routes mounted
at `/api/workspaces/{slug}/invites/...` — which collide with this carve-out
only if a workspace's slug is literally "invites". `_is_invite_link` matches
the exact two route shapes (not a blanket prefix) so that edge case can't put
the CRUD routes under this allowlist at all.
"""
from __future__ import annotations

from apps.common.middleware import _is_invite_link


class _Req:
    def __init__(self, path, method="GET"):
        self.path = path
        self.method = method


def test_preview_route_is_public():
    assert _is_invite_link(_Req("/api/workspaces/invites/tok123/preview")) is True


def test_accept_route_is_public():
    assert _is_invite_link(_Req("/api/workspaces/invites/tok123/accept", method="POST")) is True


def test_owner_crud_routes_stay_auth_gated():
    # a normal workspace slug never collides with the literal "invites" segment
    assert _is_invite_link(_Req("/api/workspaces/acme/invites/")) is False
    assert _is_invite_link(_Req("/api/workspaces/acme/invites/5/revoke", method="POST")) is False


def test_owner_crud_routes_stay_auth_gated_even_if_a_workspace_is_named_invites():
    # the pathological slug="invites" case: /api/workspaces/invites/invites/ (list)
    # and /api/workspaces/invites/invites/5/revoke (revoke) must NOT match — only
    # the exact <token>/preview and <token>/accept shapes do.
    assert _is_invite_link(_Req("/api/workspaces/invites/invites/")) is False
    assert _is_invite_link(_Req("/api/workspaces/invites/invites/5/revoke", method="POST")) is False


def test_bare_invites_collection_is_not_public():
    assert _is_invite_link(_Req("/api/workspaces/invites/")) is False
