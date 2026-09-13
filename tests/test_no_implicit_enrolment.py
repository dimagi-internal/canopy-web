"""Creating a row must never be a way to JOIN a workspace.

Five create endpoints each hand-rolled the same shape:

    ws = request.workspace_slug or ensure_default_workspace()
    ensure_member(ws, request.user)          # grants EDITOR

On the flat mount `workspace_slug` is `None`, so `ws` resolved to the org
default (`dimagi`) *regardless of who was calling*, and `ensure_member` then
made them an EDITOR of it as a side effect of the write.

This was strictly BROADER than the self-join feature it sat beside.
`join_workspace` requires the caller's email domain to be in the workspace's
`self_join_domains`; these required nothing at all. So the user this most
affects is the one the design deliberately keeps OUT of the domain allowlist:
an invite-admitted outside collaborator correctly gets `[]` from
`/api/workspaces/joinable` and a 404 from `POST /{slug}/join`, and could then
become an editor of `dimagi` by posting a single shareout — and from there pass
every `editor` gate on the agent fleet, including running turns.

It also falsified two sentences `docs/architecture/roles.md` now states
outright: that there are three ways into a workspace with "deliberately no
fourth", and that "there is no automatic join". Documentation that promises a
boundary the code does not hold is worse than no documentation, because an
administrator grants a role on the strength of it.

One test per endpoint, all asserting the same invariant on the same axis — the
caller's membership set is unchanged by the call — because the five sites are
independent copies and a fix applied to four of them is not a fix.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def outsider(default_workspace):
    """An authenticated user who belongs to NO workspace.

    Deliberately on a domain outside the allowlist, so this is the
    invite-admitted collaborator rather than a colleague who simply has not
    joined yet — the point being that no amount of domain matching could have
    let them in, and yet a single POST did.
    """
    user = get_user_model().objects.create_user(
        username="outsider", email="outsider@partner.example"
    )
    c = Client()
    c.force_login(user)
    return c, user


def _memberships(user) -> set[str]:
    return set(
        WorkspaceMembership.objects.filter(user=user).values_list("workspace_id", flat=True)
    )


def test_creating_a_project_does_not_enrol_the_caller(outsider):
    client, user = outsider
    res = client.post(
        "/api/projects/",
        {"name": "Trojan", "slug": "trojan"},
        content_type="application/json",
    )
    assert res.status_code == 422, res.content
    assert _memberships(user) == set()


def test_posting_a_shareout_does_not_enrol_the_caller(outsider):
    """The cheapest exploit of the five: one POST, no prerequisites."""
    client, user = outsider
    now = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    res = client.post(
        "/api/shareouts/",
        {
            "shareouts": [{
                "period_start": now.isoformat(),
                "period_end": now.isoformat(),
                "title": "Week of nothing",
                "content": "x",
                "source": "test",
            }],
        },
        content_type="application/json",
    )
    assert res.status_code == 422, res.content
    assert _memberships(user) == set()


def test_uploading_a_walkthrough_does_not_enrol_the_caller(outsider):
    client, user = outsider
    res = client.post("/api/walkthroughs/", {
        "file": SimpleUploadedFile("demo.html", b"<html>hi</html>", content_type="text/html"),
        "kind": "html",
        "title": "Trojan demo",
    })
    assert res.status_code == 422, res.content
    assert _memberships(user) == set()


def test_creating_a_review_does_not_enrol_the_caller(outsider):
    client, user = outsider
    res = client.post(
        "/api/reviews/",
        {"request_json": {"run_id": "r1", "narrative_slug": "n1", "gate": "g1"}},
        content_type="application/json",
    )
    assert res.status_code == 422, res.content
    assert _memberships(user) == set()


def test_filing_an_origin_record_does_not_enrol_the_caller(outsider):
    client, user = outsider
    res = client.post(
        "/api/issues/",
        {"repo": "dimagi/canopy-web", "number": 9999, "title": "t", "agent": ""},
        content_type="application/json",
    )
    assert res.status_code == 422, res.content
    assert _memberships(user) == set()


def test_an_origin_record_is_never_left_unhomed(outsider):
    """Why issues refuses rather than filing with `workspace=None`.

    `OriginIssue.workspace` is nullable and `_visible` keeps a null-workspace
    row readable by ANY authenticated caller — a legacy carve-out. So falling
    back to `None` when no tenant resolves would not merely skip the enrolment,
    it would land the row *inside* that carve-out: a globally readable record
    created by someone in no workspace. That is the NULL-means-allow shape this
    codebase has been bitten by repeatedly (see `agents/0013`).
    """
    from apps.issues.models import OriginIssue

    client, _ = outsider
    client.post(
        "/api/issues/",
        {"repo": "dimagi/canopy-web", "number": 9998, "title": "t", "agent": ""},
        content_type="application/json",
    )
    assert not OriginIssue.objects.filter(workspace__isnull=True).exists()


# --- the legitimate callers must be unaffected --------------------------------
# This fix could trivially be "correct" by breaking every real client, so the
# other half is pinned too: a member of the org default posting FLAT (which is
# what the whole PAT/plugin fleet does) must land in exactly the workspace it
# lands in today, and gain nothing.


@pytest.fixture
def insider(default_workspace):
    user = get_user_model().objects.create_user(username="insider", email="insider@dimagi.com")
    WorkspaceMembership.objects.create(
        workspace=default_workspace, user=user, role=WorkspaceMembership.EDITOR
    )
    c = Client()
    c.force_login(user)
    return c, user


def test_a_member_posting_flat_still_lands_in_the_default_workspace(insider, default_workspace):
    client, user = insider
    res = client.post(
        "/api/projects/", {"name": "Real", "slug": "real"}, content_type="application/json"
    )
    assert res.status_code == 201, res.content

    from apps.projects.models import Project

    assert Project.objects.get(slug="real").workspace_id == default_workspace.slug
    assert _memberships(user) == {default_workspace.slug}


def test_a_member_of_only_one_other_workspace_lands_there_not_in_the_default(default_workspace):
    """The leg that used to be silently wrong even for a legitimate caller.

    Someone whose sole membership is `acme` posting flat used to have their row
    filed into `dimagi` — and be enrolled there to make it work. Their own
    unambiguous workspace is the only defensible answer.
    """
    other = a_workspace("acme")
    user = get_user_model().objects.create_user(username="acme-user", email="u@acme.example")
    WorkspaceMembership.objects.create(
        workspace=other, user=user, role=WorkspaceMembership.EDITOR
    )
    c = Client()
    c.force_login(user)
    res = c.post(
        "/api/projects/", {"name": "Acme thing", "slug": "acme-thing"},
        content_type="application/json",
    )
    assert res.status_code == 201, res.content

    from apps.projects.models import Project

    assert Project.objects.get(slug="acme-thing").workspace_id == "acme"
    assert _memberships(user) == {"acme"}


def test_the_pinned_tenant_route_is_unchanged(insider, default_workspace):
    """`/api/w/{ws}/…` was never the problem — membership is gated upstream by
    `WorkspaceResolveMiddleware` before `workspace_slug` is ever set — so the
    fix must not add a second, divergent check on that path."""
    client, user = insider
    res = client.post(
        f"/api/w/{default_workspace.slug}/projects/",
        {"name": "Pinned", "slug": "pinned"},
        content_type="application/json",
    )
    assert res.status_code == 201, res.content
    assert _memberships(user) == {default_workspace.slug}


def test_ensure_member_is_no_longer_called_from_any_create_path():
    """The structural assertion, because five copies is how this happened.

    Each site grew its own copy of the enrolment shape, so fixing the ones a
    review happened to name leaves the pattern free to be copied a sixth time
    into the next create endpoint. `ensure_member` is legitimate in exactly
    three places — the deliberate ways into a workspace — and this fails on a
    new caller rather than waiting for the next audit to find it.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    allowed = {
        # The four deliberate doors, and nothing else. Each is either an
        # explicit human action or configured server-side by an owner; none can
        # be reached as a side effect of writing a row. See
        # docs/architecture/roles.md, "How you get into a workspace".
        "apps/workspaces/services.py",      # ensure_member itself + join_workspace
        "apps/workspaces/api.py",           # invite acceptance / explicit self-join
        "apps/workspaces/testing.py",       # fixtures
        # App-credential token exchange. Legitimate because the workspace comes
        # ONLY from the credential's server-side row (`provision_workspace`),
        # never from the request — an owner configured it deliberately, and the
        # grant is audited. This is the door roles.md calls the fourth.
        "apps/tokens/exchange_api.py",
    }
    offenders = []
    for path in root.glob("apps/**/*.py"):
        rel = str(path.relative_to(root))
        if rel in allowed or "/migrations/" in rel or "/tests/" in rel:
            continue
        if re.search(r"\bensure_member\s*\(", path.read_text()):
            offenders.append(rel)
    assert not offenders, (
        f"{offenders} call ensure_member outside the deliberate join paths — "
        "creating a row must not be a way to join a workspace. Use "
        "wsvc.creation_workspace(request), which resolves only tenants the "
        "caller is already in."
    )
