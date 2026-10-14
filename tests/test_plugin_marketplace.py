"""The personal agent catalog, `canopy-agents` (docs/architecture/plugin-marketplace.md).

What these pin:

- `/plugins/marketplace.json` lists every agent with a github.com repo in every
  workspace the caller belongs to — any role, any organisation — and nothing
  from a workspace they don't;
- each entry is a `github` source on the agent's repo (with `ref` only when the
  agent follows a non-default ref), so Claude Code fetches it with the user's
  OWN git credentials: canopy lends no GitHub access (its one GitHub read, the
  plugin.json name, is anonymous);
- entries are PLUGINS, not instances: agents sharing a repo + ref are one entry,
  named from plugin.json (else the repo name); a fork that reuses a name is
  listed under a qualified name, never dropped;
- no identity is a 401 naming the header (never a sign-in redirect); a user
  with no memberships gets a valid, empty catalog;
- the retired per-workspace marketplace and archive URLs answer 410 with the
  new address.
"""
from __future__ import annotations

from unittest import mock

import pytest
import requests
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings

from apps.agents.models import Agent
from apps.plugins import services
from apps.tokens.models import PersonalToken
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db

URL = "/plugins/marketplace.json"


class RawGitHub:
    """Anonymous raw.githubusercontent.com: plugin.json names for PUBLIC repos."""

    def __init__(self):
        self.names: dict[str, str] = {}   # "owner/repo" -> plugin.json name
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None, **_):
        self.calls.append((url, headers or {}))
        repo = "/".join(url.split("raw.githubusercontent.com/", 1)[1].split("/")[:2])
        r = mock.Mock()
        if repo in self.names:
            r.status_code, r.json = 200, (lambda: {"name": self.names[repo], "version": "1.0.0"})
        else:
            r.status_code = 404     # a private repo, to an anonymous reader
        return r


@pytest.fixture(autouse=True)
def raw():
    cache.clear()
    fake = RawGitHub()
    with mock.patch.object(services.requests, "get", fake.get):
        yield fake
    cache.clear()


def bearer(user) -> Client:
    raw, _ = PersonalToken.create_for_user(user=user, label="marketplace")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def agent(slug, ws, *, repo=None, ref="main", **kw) -> Agent:
    return Agent.objects.create(
        slug=slug, name=slug.title(), workspace=ws,
        repo_url=f"git@github.com:dimagi-internal/{slug}.git" if repo is None else repo,
        repo_ref=ref, **kw)


@pytest.fixture
def fleet():
    """Two organisations: dimagi (with its connect division) and family."""
    dimagi = a_workspace("dimagi")
    connect = a_workspace("connect", parent=dimagi)
    family = a_workspace("family")
    agent("ada", dimagi, description="Conducts the fleet")
    agent("ace", connect)
    agent("echo", family)
    agent("nogit", dimagi, repo="")                                   # no repo: not listed
    agent("gitlab", dimagi, repo="https://gitlab.com/x/gitlab.git")  # not github.com
    return dimagi, connect, family


def names(resp) -> list[str]:
    return [p["name"] for p in resp.json()["plugins"]]


def test_lists_agents_from_every_workspace_the_caller_belongs_to(fleet):
    dimagi, connect, family = fleet
    pat = User.objects.create_user("pat", "pat@dimagi.com", "pw")
    wsvc.ensure_member(dimagi, pat, WorkspaceMembership.VIEWER)
    wsvc.ensure_member(connect, pat, WorkspaceMembership.EDITOR)
    resp = bearer(pat).get(URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "canopy-agents"
    assert names(resp) == ["ace", "ada"]          # sorted by slug; family's echo is not his


def test_spans_organisations(fleet):
    dimagi, _, family = fleet
    pat = User.objects.create_user("pat", "pat@dimagi.com", "pw")
    wsvc.ensure_member(dimagi, pat, WorkspaceMembership.VIEWER)
    wsvc.ensure_member(family, pat, WorkspaceMembership.VIEWER)
    assert names(bearer(pat).get(URL)) == ["ada", "echo"]


def test_a_viewer_of_the_org_does_not_get_its_divisions_agents(fleet):
    dimagi, _, _ = fleet
    pat = User.objects.create_user("pat", "pat@dimagi.com", "pw")
    wsvc.ensure_member(dimagi, pat, WorkspaceMembership.VIEWER)
    assert names(bearer(pat).get(URL)) == ["ada"]


def test_an_org_owner_gets_its_divisions_agents(fleet):
    dimagi, _, _ = fleet
    olive = User.objects.create_user("olive", "olive@dimagi.com", "pw")
    wsvc.ensure_member(dimagi, olive, WorkspaceMembership.OWNER)
    assert names(bearer(olive).get(URL)) == ["ace", "ada"]


def test_entries_are_github_sources_fetched_with_the_users_own_git(fleet, raw):
    dimagi, connect, _ = fleet
    pat = User.objects.create_user("pat", "pat@dimagi.com", "pw")
    wsvc.ensure_member(dimagi, pat, WorkspaceMembership.VIEWER)
    wsvc.ensure_member(connect, pat, WorkspaceMembership.VIEWER)
    plugins = {p["name"]: p for p in bearer(pat).get(URL).json()["plugins"]}
    assert plugins["ada"] == {
        "name": "ada",
        "description": "Conducts the fleet",
        "source": {"source": "github", "repo": "dimagi-internal/ada"},
        "metadata": {"instances": [{"agent": "ada", "workspace": "dimagi"}]},
    }
    assert plugins["ace"]["description"] == "Ace"   # falls back to the agent's name
    # No archive, no sha256, no entry version: Claude Code tracks the repo.
    assert all("version" not in p for p in plugins.values())
    # canopy's only GitHub read is anonymous: it lends nobody's credential.
    assert raw.calls and all("Authorization" not in h for _, h in raw.calls)


def test_two_instances_of_one_repo_are_one_plugin(raw):
    dimagi, family = a_workspace("dimagi"), a_workspace("family")
    agent("ace", dimagi, repo="git@github.com:dimagi-internal/ace.git")
    agent("ace-family", family, repo="https://github.com/dimagi-internal/ace")
    pat = User.objects.create_user("pat", "pat@dimagi.com", "pw")
    wsvc.ensure_member(dimagi, pat, WorkspaceMembership.VIEWER)
    wsvc.ensure_member(family, pat, WorkspaceMembership.VIEWER)
    [only] = bearer(pat).get(URL).json()["plugins"]
    assert only["name"] == "ace"                      # the repo name, not a slug
    assert only["source"] == {"source": "github", "repo": "dimagi-internal/ace"}
    assert only["metadata"]["instances"] == [
        {"agent": "ace", "workspace": "dimagi"},
        {"agent": "ace-family", "workspace": "family"},
    ]


def test_the_name_comes_from_plugin_json_whichever_instance_you_see(raw):
    raw.names["dimagi-internal/connect-ace"] = "ace"
    family = a_workspace("family")
    agent("family-helper", family, repo="git@github.com:dimagi-internal/connect-ace.git")
    pat = User.objects.create_user("pat", "pat@dimagi.com", "pw")
    wsvc.ensure_member(family, pat, WorkspaceMembership.VIEWER)
    assert names(bearer(pat).get(URL)) == ["ace"]


def test_the_plugin_name_is_cached_and_a_github_failure_falls_back(raw):
    ws = a_workspace("dimagi")
    a = agent("hal", ws)
    with mock.patch.object(services.requests, "get", side_effect=requests.ConnectionError):
        assert services.plugin_name(a) == "hal"
    raw.names["dimagi-internal/hal"] = "hal-renamed"
    assert services.plugin_name(a) == "hal"           # the miss is cached too
    cache.clear()
    assert services.plugin_name(a) == "hal-renamed"


def test_a_fork_reusing_a_name_is_listed_under_a_qualified_name(raw, caplog):
    raw.names["someone/ace"] = "ace"
    dimagi, family = a_workspace("dimagi"), a_workspace("family")
    agent("ace", dimagi)
    agent("ace-fork", family, repo="git@github.com:someone/ace.git")
    agent("ace-beta", dimagi, repo="git@github.com:dimagi-internal/ace.git", ref="beta")
    pat = User.objects.create_user("pat", "pat@dimagi.com", "pw")
    wsvc.ensure_member(dimagi, pat, WorkspaceMembership.VIEWER)
    wsvc.ensure_member(family, pat, WorkspaceMembership.VIEWER)
    plugins = {p["name"]: p["source"] for p in bearer(pat).get(URL).json()["plugins"]}
    assert plugins == {
        "ace": {"source": "github", "repo": "dimagi-internal/ace"},     # oldest instance
        "ace--someone-ace": {"source": "github", "repo": "someone/ace"},
        "ace--dimagi-internal-ace--beta":
            {"source": "github", "repo": "dimagi-internal/ace", "ref": "beta"},
    }
    assert "ace--someone-ace" in caplog.text


@pytest.mark.parametrize("ref,expected", [
    ("main", {"source": "github", "repo": "dimagi-internal/hal"}),
    ("", {"source": "github", "repo": "dimagi-internal/hal"}),
    ("stable", {"source": "github", "repo": "dimagi-internal/hal", "ref": "stable"}),
])
def test_ref_only_when_the_agent_follows_a_non_default_ref(ref, expected):
    ws = a_workspace("dimagi")
    assert services.plugin_source(agent("hal", ws, ref=ref)) == expected


def test_any_github_url_spelling_becomes_owner_repo():
    ws = a_workspace("dimagi")
    a = agent("eva", ws, repo="https://github.com/dimagi-internal/eva")
    assert services.plugin_source(a)["repo"] == "dimagi-internal/eva"


def test_no_memberships_is_a_valid_empty_catalog(fleet):
    loner = User.objects.create_user("lee", "lee@dimagi.com", "pw")
    resp = bearer(loner).get(URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "canopy-agents" and body["owner"]["name"] and body["plugins"] == []
    assert resp["Cache-Control"] == "private, no-cache"


@override_settings(REQUIRE_AUTH=True)
def test_no_identity_is_a_401_that_names_the_header_not_a_sign_in_redirect(fleet):
    resp = Client().get(URL)
    assert resp.status_code == 401
    assert resp["WWW-Authenticate"].startswith("Bearer")
    assert "Authorization: Bearer" in resp.json()["detail"]


@override_settings(REQUIRE_AUTH=True)
@pytest.mark.parametrize("path", [
    "/w/family/marketplace.json",
    "/w/connect/plugins/ace/aaaaaaaaaaaa.zip",
    "/w/nope/marketplace.json",
])
@pytest.mark.parametrize("signed_in", [False, True])
def test_the_retired_per_workspace_urls_are_410_naming_the_new_catalog(fleet, path, signed_in):
    c = Client()
    if signed_in:
        c = bearer(User.objects.create_user("pat", "pat@dimagi.com", "pw"))
    resp = c.get(path)
    assert resp.status_code == 410
    body = resp.json()
    assert body["url"].endswith("/plugins/marketplace.json")
    assert body["marketplace"] == "canopy-agents"
    assert "moved to one catalog" in body["detail"] and "/canopy:setup" in body["detail"]
