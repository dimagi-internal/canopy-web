"""One bearer resolver for REST, MCP and the WebSocket handshake (`apps/tokens/principal.py`)."""
from __future__ import annotations

import re

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.realtime import channels_auth
from apps.tokens import principal as P
from apps.tokens.models import AppCredential, DelegatedToken, PersonalToken
from tests._source import ROOT
from tests.site_tenant import host_workspace

pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user("u", "u@dimagi.com", "pw")


@pytest.fixture()
def pat(user):
    raw, token = PersonalToken.create_for_user(user=user, label="cli")
    return raw, token


@pytest.fixture()
def delegated(user):
    cred = AppCredential.create_credential(name="a", created_by=user, workspace=host_workspace())
    raw, token = DelegatedToken.issue(app=cred, user=user, ttl_seconds=600)
    return raw, token


def test_a_pat_resolves_and_its_use_is_stamped_with_the_client(user, pat):
    raw, token = pat
    found = P.resolve_bearer(raw, client="canopy-cli")
    assert found.kind == P.PAT and found.user == user
    assert found.credential == {"type": "pat", "id": token.pk, "label": "cli"}
    token.refresh_from_db()
    assert token.last_used_at is not None and token.last_used_client == "canopy-cli"


def test_a_recheck_stamps_nothing(pat):
    raw, token = pat
    assert P.resolve_bearer(raw, client=None) is not None
    token.refresh_from_db()
    assert token.last_used_at is None


def test_a_delegated_token_resolves_with_its_assurance(user, delegated):
    raw, token = delegated
    found = P.resolve_bearer(raw)
    assert found.kind == P.DELEGATED and found.user == user
    assert found.auth_method == (token.assurance or "delegated")


def test_only_the_accepted_kinds_are_looked_up(delegated):
    """A door that takes one kind pays for one query, and never accepts another."""
    raw, _ = delegated
    assert P.resolve_bearer(raw, accept={P.PAT}) is None
    with CaptureQueriesContext(connection) as q:
        assert P.resolve_bearer(raw, accept={P.DELEGATED}) is not None
    assert not any("personal_tokens" in x["sql"] or "contact_tokens" in x["sql"]
                   for x in q.captured_queries)


def test_garbage_resolves_to_nothing():
    assert P.resolve_bearer("") is None
    assert P.resolve_bearer("not-a-token") is None


def test_the_mcp_verifier_stamps_a_pat_like_rest_does(user, pat):
    """MCP used to stamp only `last_used_at`; it now records the client too."""
    from apps.mcp.auth import CanopyPATVerifier

    raw, token = pat
    access = async_to_sync(CanopyPATVerifier().verify_token)(raw)
    assert access.claims["user_id"] == user.pk
    token.refresh_from_db()
    assert token.last_used_at is not None and token.last_used_client


def test_the_mcp_verifier_takes_no_delegated_token(delegated):
    from apps.mcp.auth import CanopyPATVerifier

    raw, _ = delegated
    assert async_to_sync(CanopyPATVerifier().verify_token)(raw) is None


def test_a_delegated_socket_looks_its_token_up_once(user, delegated):
    """The handshake used to look a delegated bearer up as many as five times
    (identity, method, site, runner requirements, widget check)."""
    raw, _ = delegated
    seen = {}

    async def app(scope, receive, send):
        seen.update(scope)

    scope = {"type": "websocket", "path": "/ws/chat/x/", "query_string": b"",
             "headers": [(b"authorization", f"Bearer {raw}".encode())]}
    mw = channels_auth.RealtimeAuthMiddleware(app)
    with CaptureQueriesContext(connection) as q:
        async_to_sync(mw)(scope, None, None)
    lookups = [x for x in q.captured_queries
               if re.search(r'FROM "delegated_tokens"|FROM delegated_tokens', x["sql"])]
    assert len(lookups) == 1, [x["sql"] for x in lookups]
    assert seen["via_widget"] is True


def test_the_three_doors_resolve_bearers_only_through_the_resolver():
    """Token lookups live in `principal.py`; a door that calls `.lookup(` on a
    token model has started its own resolution order again."""
    doors = ["apps/tokens/middleware.py", "apps/mcp/auth.py", "apps/realtime/channels_auth.py"]
    for door in doors:
        text = (ROOT / door).read_text()
        assert not re.search(r"(PersonalToken|DelegatedToken|ContactToken|RunnerAccessToken)\.lookup\(",
                             text), door


def test_token_secrets_are_hashed_by_the_one_helper():
    """`apps.common.token_hash.hash_token` is how a token-shaped secret is stored."""
    offenders = []
    for path in (ROOT / "apps").rglob("*.py"):
        if "tests" in path.parts or "migrations" in path.parts or path.name == "token_hash.py":
            continue
        if re.search(r"hashlib\.sha256\((raw|ticket|token|secret)\.encode\(\)\)\.hexdigest", path.read_text()):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
