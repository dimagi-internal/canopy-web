"""A workspace's agent plugins, served as a Claude Code marketplace (canopy-web#1376).

What these pin:

- `/w/<ws>/marketplace.json` lists one `archive` entry per agent of the
  workspace that has a GitHub repo, each pinned by the sha256 of the bytes its
  URL serves — and the URL serves exactly those bytes;
- the archive's root is the plugin root, and its plugin.json version carries the
  commit, so a merge is always a version change Claude Code sees;
- a merge moves the head and the next read (after the head TTL) lists the new
  version, while the old URL keeps serving the old bytes;
- GitHub is asked with the agent OWNER's delegated credential;
- no canopy identity is a 401 (never a sign-in redirect), a non-member a 404,
  an agent of another workspace a 404;
- a build that fails, or that the time budget does not allow, falls back to the
  last built version rather than failing the marketplace.

GitHub is never called: `requests.get` is replaced by a fake repo host.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, override_settings

from apps.agents.models import Agent, AgentDelegation
from apps.common.encryption import encrypt_secret
from apps.plugins import services
from apps.plugins.models import PluginArchive
from apps.tokens.models import PersonalToken
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db

SHA1 = "a" * 40
SHA2 = "b" * 40


def zipball(top: str, files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{top}/", b"")
        for name, body in files.items():
            z.writestr(f"{top}/{name}", body)
    return buf.getvalue()


def plugin_files(name="echo", version="1.2.0", desc="Echo tells stories"):
    return {
        ".claude-plugin/plugin.json": json.dumps({"name": name, "version": version, "description": desc}),
        "skills/turn/SKILL.md": "# turn\n",
        "README.md": "hi\n",
    }


class FakeGitHub:
    def __init__(self):
        self.heads: dict[str, str] = {}         # "owner/repo@ref" -> sha
        self.zips: dict[str, bytes] = {}        # "owner/repo@sha" -> zipball
        self.calls: list[tuple[str, str]] = []  # (url, Authorization)
        self.fail = False

    def get(self, url, headers=None, **_):
        self.calls.append((url, (headers or {}).get("Authorization", "")))
        path = url.split("/repos/", 1)[1]
        r = mock.Mock()
        if self.fail:
            r.status_code = 502
            return r
        if "/commits/" in path:
            repo, ref = path.split("/commits/")
            sha = self.heads.get(f"{repo}@{ref}")
            r.status_code, r.text = (200, sha) if sha else (404, "")
            return r
        repo, sha = path.split("/zipball/")
        body = self.zips.get(f"{repo}@{sha}")
        r.status_code = 200 if body else 404
        r.iter_content = lambda n: [body] if body else []
        return r


@pytest.fixture
def github():
    cache.clear()
    fake = FakeGitHub()
    with mock.patch.object(services.requests, "get", fake.get):
        yield fake
    cache.clear()


@pytest.fixture
def owner():
    return User.objects.create_user("olive", "olive@dimagi.com", "pw")


@pytest.fixture
def ws(owner):
    w = a_workspace("family")
    wsvc.ensure_member(w, owner, WorkspaceMembership.OWNER)
    return w


@pytest.fixture
def echo(ws, owner, github):
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=owner,
                                 repo_url="git@github.com:dimagi-internal/echo.git")
    AgentDelegation.objects.create(user=owner, agent=agent, service=AgentDelegation.GITHUB,
                                   secret_enc=encrypt_secret("github_pat_olive"), meta={})
    github.heads["dimagi-internal/echo@main"] = SHA1
    github.zips[f"dimagi-internal/echo@{SHA1}"] = zipball("dimagi-internal-echo-aaaaaaa", plugin_files())
    return agent


def bearer(user) -> Client:
    raw, _ = PersonalToken.create_for_user(user=user, label="marketplace")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def a_viewer(ws, email="vee@example.org"):
    u = User.objects.create_user(email, email, "pw")
    wsvc.ensure_member(ws, u, WorkspaceMembership.VIEWER)
    return u


# ---- the catalog ------------------------------------------------------------------

def test_marketplace_lists_each_agent_as_a_pinned_archive(ws, echo, github):
    c = bearer(a_viewer(ws))  # a viewer, with no GitHub of their own
    resp = c.get("/w/family/marketplace.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "canopy-family"
    assert body["owner"]["name"] == "Family"
    [entry] = body["plugins"]
    assert entry["name"] == "echo"
    assert entry["version"] == f"1.2.0+{SHA1[:12]}"
    assert entry["description"] == "Echo tells stories"
    src = entry["source"]
    assert src["source"] == "archive"
    assert src["url"].endswith(f"/w/family/plugins/echo/{SHA1[:12]}.zip")
    assert src["url"].startswith("https://") or src["url"].startswith("http")
    assert entry["metadata"] == {"agent": "echo", "repo": "dimagi-internal/echo", "commit": SHA1}

    # The URL serves exactly the bytes the sha256 pins.
    path = src["url"].split("://", 1)[1].split("/", 1)[1]
    zresp = c.get("/" + path)
    assert zresp.status_code == 200
    assert zresp["Content-Type"] == "application/zip"
    assert hashlib.sha256(zresp.content).hexdigest() == src["sha256"]


def test_github_is_asked_as_the_agent_owner(ws, echo, github):
    bearer(a_viewer(ws)).get("/w/family/marketplace.json")
    assert github.calls and all(auth == "Bearer github_pat_olive" for _, auth in github.calls)


def test_the_archive_root_is_the_plugin_root_and_its_version_carries_the_commit(ws, echo, github):
    archive = PluginArchive.objects.get(pk=services.build(echo, SHA1).pk)
    z = zipfile.ZipFile(io.BytesIO(bytes(archive.content)))
    names = set(z.namelist())
    assert ".claude-plugin/plugin.json" in names
    assert "skills/turn/SKILL.md" in names
    assert not any(n.startswith("dimagi-internal-echo") for n in names)
    manifest = json.loads(z.read(".claude-plugin/plugin.json"))
    assert manifest["version"] == f"1.2.0+{SHA1[:12]}"
    assert manifest["name"] == "echo"


def test_a_plugin_in_a_marketplace_subdirectory_is_found():
    files = {
        ".claude-plugin/marketplace.json": json.dumps(
            {"name": "x", "plugins": [{"name": "hal", "source": "./plugins/hal"}]}),
        "plugins/hal/.claude-plugin/plugin.json": json.dumps({"name": "hal"}),
        "plugins/hal/skills/a/SKILL.md": "a",
        "docs/other.md": "not in the plugin",
    }
    built = services.repackage(zipball("org-hal-123", files), SHA1, "hal")
    names = set(zipfile.ZipFile(io.BytesIO(built.content)).namelist())
    assert names >= {".claude-plugin/plugin.json", "skills/a/SKILL.md"}
    assert "docs/other.md" not in names
    assert built.subdir == "plugins/hal"
    assert built.version == f"0.0.0+{SHA1[:12]}"


def test_a_repo_with_no_plugin_is_a_build_error():
    with pytest.raises(services.BuildError, match="plugin.json"):
        services.repackage(zipball("org-x-1", {"README.md": "x"}), SHA1, "x")


def test_a_merge_lists_the_new_version_and_the_old_url_still_serves(ws, echo, github):
    c = bearer(a_viewer(ws))
    first = c.get("/w/family/marketplace.json").json()["plugins"][0]
    github.heads["dimagi-internal/echo@main"] = SHA2
    github.zips[f"dimagi-internal/echo@{SHA2}"] = zipball(
        "dimagi-internal-echo-bbbbbbb", plugin_files(desc="Echo, improved"))

    # Within the head TTL the marketplace does not ask GitHub again.
    assert c.get("/w/family/marketplace.json").json()["plugins"][0]["version"] == first["version"]

    cache.clear()  # the TTL passes
    second = c.get("/w/family/marketplace.json").json()["plugins"][0]
    assert second["version"] == f"1.2.0+{SHA2[:12]}"
    assert second["source"]["sha256"] != first["source"]["sha256"]
    assert second["description"] == "Echo, improved"
    old_path = first["source"]["url"].split("/w/", 1)[1]
    assert c.get(f"/w/{old_path}").status_code == 200


def test_a_failed_build_falls_back_to_the_last_built_version(ws, echo, github):
    c = bearer(a_viewer(ws))
    first = c.get("/w/family/marketplace.json").json()["plugins"][0]
    cache.clear()
    github.fail = True
    assert c.get("/w/family/marketplace.json").json()["plugins"] == [first]


def test_past_the_budget_an_unbuilt_head_is_served_at_its_last_version(ws, echo, github):
    services.build(echo, SHA1)
    github.heads["dimagi-internal/echo@main"] = SHA2
    github.zips[f"dimagi-internal/echo@{SHA2}"] = zipball("e-b", plugin_files())
    body = services.marketplace(ws, budget=0)
    assert body["plugins"][0]["metadata"]["commit"] == SHA1
    assert not PluginArchive.objects.filter(commit_sha=SHA2).exists()


def test_agents_without_a_github_repo_are_left_out(ws, owner, echo, github):
    Agent.objects.create(slug="local", name="Local", workspace=ws, owner=owner, repo_url="")
    Agent.objects.create(slug="gl", name="GL", workspace=ws, owner=owner,
                         repo_url="https://gitlab.com/x/y")
    names = [p["name"] for p in services.marketplace(ws)["plugins"]]
    assert names == ["echo"]


def test_a_management_command_prewarms(ws, echo, github, capsys):
    call_command("build_plugin_archives", "--workspace", "family")
    assert PluginArchive.objects.filter(repo="dimagi-internal/echo", commit_sha=SHA1).exists()
    assert "family/echo: echo 1.2.0+" in capsys.readouterr().out


# ---- who may read it ------------------------------------------------------------------

@override_settings(REQUIRE_AUTH=True)
@pytest.mark.parametrize("path", [
    "/w/family/marketplace.json",
    f"/w/family/plugins/echo/{SHA1[:12]}.zip",
])
def test_no_identity_is_a_401_that_names_the_header_not_a_sign_in_redirect(ws, echo, path):
    resp = Client().get(path)
    assert resp.status_code == 401
    assert resp["WWW-Authenticate"].startswith("Bearer")
    assert "Authorization: Bearer" in resp.json()["detail"]


@override_settings(REQUIRE_AUTH=True)
def test_a_non_member_gets_the_same_404_as_no_such_workspace(ws, echo, github):
    stranger = User.objects.create_user("sam", "sam@dimagi.com", "pw")
    c = bearer(stranger)
    services.build(echo, SHA1)
    assert c.get("/w/family/marketplace.json").status_code == 404
    assert c.get(f"/w/family/plugins/echo/{SHA1[:12]}.zip").status_code == 404
    assert c.get("/w/nope/marketplace.json").status_code == 404


def test_an_agent_of_another_workspace_is_not_served_under_this_one(ws, echo, github, owner):
    services.build(echo, SHA1)
    other = a_workspace("other")
    wsvc.ensure_member(other, owner, WorkspaceMembership.OWNER)
    c = bearer(owner)
    assert c.get(f"/w/other/plugins/echo/{SHA1[:12]}.zip").status_code == 404
    assert c.get(f"/w/family/plugins/echo/{SHA1[:12]}.zip").status_code == 200


@pytest.mark.parametrize("version", ["abc", "g123456789", "ZZZZZZZZ", SHA2[:12]])
def test_a_bad_or_unknown_version_is_a_404(ws, echo, github, owner, version):
    services.build(echo, SHA1)
    assert bearer(owner).get(f"/w/family/plugins/echo/{version}.zip").status_code == 404
