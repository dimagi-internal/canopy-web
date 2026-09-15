"""Fitness test: `/api/contact/` is the WHOLE of what a contact can reach.

The sibling of `test_workspace_authorizer_is_sole_gate.py`, at the other
principal. That one guards "is this USER allowed in this WORKSPACE?"; a contact
has no membership at all, so it has no query of that shape to write and nothing
in it can protect this boundary.

WHY IT NEEDS ONE. The contact boundary rests on two structural facts:

  1. a contact token sets no `request.user`, so `LoginRequiredMiddleware`
     refuses every path except the prefixes it allowlists; and
  2. `/api/contact/` is the only prefix on that list that a contact can satisfy.

Both were true when written and both are one careless edit from false — a path
added to the tuple, or a route moved under the prefix without `contact_auth`.
That is exactly the shape this repo already paid for: CLAUDE.md records six
tenancy predicates that each independently grew a "no tenant ⇒ allow" leg,
because the first site was careful and the sixth was not. The fix for a rule
that keeps being re-derived is to make re-deriving it fail the build.

DELIBERATELY STRUCTURAL. These read the router and the allowlist rather than
exercising HTTP, so they cannot pass by accident when a route is unreachable
for some unrelated reason — the failure mode `test_contact_websocket.py` hit,
where every case "passed" because nothing could connect at all.
"""

from __future__ import annotations

import pytest

from apps.common.middleware import PUBLIC_PATH_PREFIXES
from apps.tokens.contact_api import contact_token_router


#: The one route here that is deliberately open — the signed assertion IS the
#: credential, so there is nobody to authenticate before checking it.
MINTING_ROUTE = "/api/auth/contact-token"


def contact_routes() -> list[tuple[str, str]]:
    """Every mounted `/api/contact/…` route, as `(method, url)`.

    Read off the API rather than listed by hand, so a route added later is
    covered the moment it exists — which is the whole point of a fitness test.
    """
    from apps.api.api import api

    found: list[tuple[str, str]] = []
    for prefix, router in api._routers:
        for path, view in router.path_operations.items():
            full = f"/api{'/' + prefix.strip('/') if prefix else ''}{path}"
            if not full.startswith("/api/contact/"):
                continue
            url = full.replace("{session_id}", "11111111-1111-1111-1111-111111111111")
            for op in view.operations:
                for method in op.methods:
                    found.append((method, url))
    return found


@pytest.mark.django_db
def test_no_contact_route_is_open_to_an_anonymous_caller():
    """The property the whole boundary rests on, tested by ASKING.

    `/api/contact/` is allowlisted in the login middleware — a contact token
    leaves the request anonymous, so it has to be. That means a route here
    which forgot its auth would be open not merely to contacts but to ANYONE,
    and this is the one way the surface fails open.

    Behavioural rather than structural because the structural version was
    wrong twice: Ninja does not populate `auth_callbacks` on the router or on
    the mounted operation, so a check reading it flagged every route, including
    the guarded ones. Asking the route is the thing that cannot be fooled by
    where a framework happens to keep its state.
    """
    from django.test import Client

    routes = contact_routes()
    # Guards the failure mode this file exists to avoid: a fitness test that
    # matched nothing would pass forever and protect nothing.
    assert len(routes) >= 5, f"the route walk found almost nothing: {routes}"

    client = Client()
    open_to_anyone = []
    for method, url in routes:
        send = getattr(client, method.lower())
        # A body only where one belongs: Django's test client reads `data` on a
        # GET as a QUERY STRING and chokes trying to parse "{}" into pairs.
        response = (
            send(url)
            if method == "GET"
            else send(url, data="{}", content_type="application/json")
        )
        if response.status_code not in (401, 403):
            open_to_anyone.append(f"{method} {url} -> {response.status_code}")

    assert not open_to_anyone, (
        "these routes answered an anonymous caller, and /api/contact/ is public "
        f"in the login middleware, so they are open to everyone: {open_to_anyone}"
    )


@pytest.mark.django_db
def test_the_check_above_can_actually_fail():
    """The minting route IS open, deliberately — so it is the proof that an
    open route looks different from a guarded one to the check above."""
    from django.test import Client

    response = Client().post(
        MINTING_ROUTE, data={"assertion": "nope"}, content_type="application/json"
    )
    assert response.status_code not in (401, 403), (
        "the deliberately-open route now refuses anonymous callers the same way "
        "a guarded one does, so the test above can no longer tell them apart"
    )


def test_the_minting_route_is_the_only_unauthenticated_one():
    """`/contact-token` is deliberately `auth=None` — the signed assertion IS
    the credential. It lives on its own router so that being unauthenticated is
    a property of one named route rather than a flag someone can copy onto a
    neighbour."""
    paths = list(contact_token_router.path_operations)
    assert paths == ["/contact-token"], (
        f"the unauthenticated contact router grew routes: {paths}. Anything else "
        "belongs on contact_router, behind contact_auth."
    )


def test_only_one_prefix_admits_a_contact():
    """A contact token leaves `request.user` anonymous, so the login
    middleware's allowlist is the entire set of paths it could ever reach.

    Listed here so that ADDING to that tuple is a deliberate act with a test to
    update, rather than a line that slips through review.
    """
    contact_reachable = [p for p in PUBLIC_PATH_PREFIXES if p.startswith("/api/contact")]
    assert contact_reachable == ["/api/contact/"], (
        f"the contact-reachable prefixes changed: {contact_reachable}"
    )


def test_the_tenant_admin_list_is_not_reachable():
    """`/api/contacts/` is the workspace's list of PEOPLE — a tenant-admin
    surface. It differs from the contact surface by one character, and only the
    allowlist entry's trailing slash keeps them apart. The middleware documents
    the same near-miss for "/about"; this pins it for the pair that actually
    exists."""
    assert not any("/api/contacts/".startswith(p) for p in PUBLIC_PATH_PREFIXES)


@pytest.mark.django_db
def test_a_contact_principal_never_becomes_a_user():
    """The fact everything above rests on, asserted at the source.

    If the middleware ever put a contact into `request.user` — to make some
    view "just work" — every one of these boundaries would dissolve at once,
    and nothing else here would notice.
    """
    import inspect

    from apps.tokens import middleware

    source = inspect.getsource(middleware.BearerTokenAuthMiddleware._authenticate)
    contact_block = source[source.index("ctok = ContactToken.lookup"):]
    contact_block = contact_block[: contact_block.index("dtok = DelegatedToken.lookup")]
    assert "request.user" not in contact_block, (
        "the contact branch now assigns request.user; every /api/contact/ "
        "boundary depends on it NOT doing that"
    )
