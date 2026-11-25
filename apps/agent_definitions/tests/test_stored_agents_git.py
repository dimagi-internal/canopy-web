"""A stored agent's definition is a git repository canopy hosts: revisions are commits.

What is pinned, each with its refusal:
- every revision is a commit: publishing writes the generated files into the commit
  `main` and `r<N>` name, a draft is `refs/heads/draft`, history is parented;
- `tree`, `root_sha256` and the archive still answer as they did (older CLIs read them);
- `definition.git_url` is on the agent detail, `/runtime`, the definition and resolve;
- the smart HTTP endpoint serves reads only: no receive-pack for anyone; only a PAT
  (a person's with read, or the agent's own login) or a runner token whose box may hold
  the agent; contact, caller (confined) and other tokens are refused like none; a
  runner never sees a draft or the imported history; the token is never logged;
- convert keeps a bundle's history under `refs/heads/imported`, after scanning every
  blob in it for secrets; too much is 413;
- export pushes `main`, its history and tags to an EMPTY repository and switches the
  agent to GitHub; it and detach are a person's act by an admin; afterwards every
  write is 409 `exported`.
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
import uuid

import pytest
from django.test import Client
from dulwich.protocol import pkt_line

from apps.agent_definitions import services
from tests.fixtures.stored_agents import (
    SLUG,
    _browser,
    _draft,
    _files,
    _post,
    _runner_client,
)
from apps.agents import definition_git as git
from apps.agents import definition_store as store
from apps.agents.models import Agent, AgentAdmin, AgentDefinitionRevision, AgentGitRef
from apps.harness import caller_tokens
from apps.harness.models import Runner, Turn
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member
from tests.token_helpers import pat_client, pat_token

pytestmark = pytest.mark.django_db


def _objects(agent):
    return store.object_store(agent)


def _commit(agent, sha):
    return _objects(agent)[sha.encode()]


def _refs(agent):
    return git.refs_of(agent.pk)


# --- revisions are commits ---------------------------------------------------


def test_creating_a_stored_agent_makes_its_repository(w, agent):
    rev = agent.live_revision
    refs = _refs(agent)
    assert refs == {git.MAIN: rev.commit, git.tag_ref(1): rev.commit}
    commit = _commit(agent, rev.commit)
    assert commit.parents == []
    assert commit.committer == git.COMMITTER
    assert commit.author == git.person_author(w["owner"])
    # The commit's tree IS the revision: the manifest fields are read from it.
    files = git.files_at(_objects(agent), rev.commit)
    assert git.manifest(files) == rev.tree
    assert files[f"bin/{SLUG}-email"][1] is True
    assert json.loads(files[".claude-plugin/plugin.json"][0])["version"] == "0.1.0"


def test_a_draft_is_the_draft_ref_and_publishing_writes_main_and_a_tag(w, agent):
    r = _draft(pat_client(w["bot"]))  # the agent's own login
    assert r.status_code == 201, r.content
    draft = AgentDefinitionRevision.objects.get(agent=agent, number=2)
    live = agent.live_revision
    assert r.json()["commit"] == draft.commit
    assert _refs(agent)[git.DRAFT] == draft.commit and _refs(agent)[git.MAIN] == live.commit
    drafted = _commit(agent, draft.commit)
    assert drafted.parents == [live.commit.encode()]
    assert drafted.author == git.agent_author(SLUG) == b"scout <agent@canopy.dimagi.com>"
    assert drafted.message.decode().strip() == "add trail"

    # The agent's name changes before a person publishes: the generated files are written
    # again, into the commit `main` and `r2` name, parented on the live commit.
    Agent.objects.filter(pk=agent.pk).update(description="Finds trails and maps them.")
    assert _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/revisions/2/publish").status_code == 200
    agent.refresh_from_db()
    published = agent.live_revision
    refs = _refs(agent)
    assert git.DRAFT not in refs
    assert refs[git.MAIN] == refs[git.tag_ref(2)] == published.commit
    assert refs[git.tag_ref(1)] == live.commit
    commit = _commit(agent, published.commit)
    assert commit.parents == [live.commit.encode()]
    assert commit.author == drafted.author  # whoever drafted it stays its author
    files = git.files_at(_objects(agent), published.commit)
    assert json.loads(files[".claude-plugin/plugin.json"][0])["version"] == "0.2.0"
    assert "maps them" in json.loads(files[".claude-plugin/plugin.json"][0])["description"]
    assert git.manifest(files) == published.tree
    assert published.root_sha256 == store.root_of(published.tree)


def test_discard_drops_the_draft_ref_and_rollback_commits_on_main(w, agent):
    assert _draft(_browser(w["editor"])).status_code == 201
    assert _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/revisions/2/discard").status_code == 200
    assert git.DRAFT not in _refs(agent)
    assert _draft(_browser(w["editor"])).status_code == 201
    assert _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/revisions/3/publish").status_code == 200
    r = _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/rollback", {"revision": 1})
    assert r.status_code == 201
    agent.refresh_from_db()
    four = agent.live_revision
    three = AgentDefinitionRevision.objects.get(agent=agent, number=3)
    assert _commit(agent, four.commit).parents == [three.commit.encode()]
    assert {n for n in _refs(agent) if n.startswith(git.TAG_PREFIX)} == {"refs/tags/r1", "refs/tags/r3",
                                                                         "refs/tags/r4"}


def test_a_refused_draft_leaves_no_ref_behind(w, agent):
    r = _draft(_browser(w["editor"]), changes=[{"path": "hooks/x.json", "content": "{}"}])
    assert r.status_code == 422
    assert git.DRAFT not in _refs(agent)


def test_older_clis_still_get_tree_root_and_archive(w, agent):
    c = _browser(w["viewer"])
    rev = c.get(f"/api/agents/{SLUG}/definition/revisions/1").json()
    assert rev["tree"] == agent.live_revision.tree and rev["root_sha256"] == agent.live_revision.root_sha256
    archive = c.get(f"/api/agents/{SLUG}/definition/archive")
    assert archive.status_code == 200 and archive["X-Canopy-Root-Sha256"] == agent.live_revision.root_sha256


def test_git_url_is_on_the_detail_the_runtime_and_the_definition(w, agent, settings):
    settings.CANOPY_PUBLIC_BASE_URL = "https://canopy.example.org/"
    url = f"https://canopy.example.org/git/scouts/{SLUG}.git"
    c = _browser(w["viewer"])
    assert c.get(f"/api/agents/{SLUG}/").json()["definition"]["git_url"] == url
    assert c.get(f"/api/agents/{SLUG}/runtime").json()["definition"]["git_url"] == url
    d = c.get(f"/api/agents/{SLUG}/definition").json()
    assert d["git_url"] == url and d["live"]["commit"] == agent.live_revision.commit


def test_a_repo_agent_has_no_git_url(w, repo_agent):
    d = _browser(w["owner"]).get("/api/agents/ranger/runtime").json()["definition"]
    assert d["source"] == "repo" and d["git_url"] is None


# --- reading over git: the endpoint --------------------------------------------


def _basic(token: str, user: str = "x") -> dict:
    return {"HTTP_AUTHORIZATION": "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()}


def _bearer(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _info_refs(headers: dict, slug=SLUG, ws="scouts", service="git-upload-pack"):
    return Client().get(f"/git/{ws}/{slug}.git/info/refs?service={service}", **headers)


def _advertised(resp) -> dict[str, str]:
    """{ref: sha} from an upload-pack advertisement (pkt-lines `<sha> <ref>[\\0caps]`)."""
    body = b"".join(resp.streaming_content) if resp.streaming else resp.content
    refs, i = {}, 0
    while i < len(body):
        size = int(body[i:i + 4], 16)
        if size == 0:
            i += 4
            continue
        line = body[i + 4:i + size].rstrip(b"\n").split(b"\0")[0]
        i += size
        if line.startswith(b"#"):
            continue
        sha, _, name = line.partition(b" ")
        refs[name.decode()] = sha.decode()
    return refs


def test_a_member_reads_the_refs_with_basic_or_bearer(w, agent, settings):
    settings.REQUIRE_AUTH = True  # the endpoint must work behind LoginRequiredMiddleware
    token = pat_token(w["viewer"])
    for headers in (_basic(token), _bearer(token)):
        r = _info_refs(headers)
        assert r.status_code == 200, r.content
        assert r["Content-Type"] == "application/x-git-upload-pack-advertisement"
        refs = _advertised(r)
        assert refs[git.MAIN] == agent.live_revision.commit and git.tag_ref(1) in refs


def test_no_token_is_a_401_that_makes_git_ask(w, agent, settings):
    settings.REQUIRE_AUTH = True
    r = _info_refs({})
    assert r.status_code == 401 and r["WWW-Authenticate"] == 'Basic realm="canopy"'
    # A browser session is not a credential here.
    c = Client()
    c.force_login(w["owner"])
    assert c.get(f"/git/scouts/{SLUG}.git/info/refs?service=git-upload-pack").status_code == 401
    assert _info_refs(_basic("not-a-token")).status_code == 401


def test_nobody_may_push_whatever_they_hold(w, agent):
    token = pat_token(w["owner"])
    for headers in ({}, _basic(token)):
        r = _info_refs(headers, service="git-receive-pack")
        assert r.status_code == 200
        assert r["Content-Type"] == "application/x-git-receive-pack-advertisement"
        assert pkt_line(b"ERR " + services_push_refused()) in r.content
        post = Client().post(f"/git/scouts/{SLUG}.git/git-receive-pack", data=b"0000",
                             content_type="application/x-git-receive-pack-request", **headers)
        assert post.status_code == 403 and b"canopy agent definition edit" in post.content
    assert _refs(agent) == {git.MAIN: agent.live_revision.commit, git.tag_ref(1): agent.live_revision.commit}


def services_push_refused() -> bytes:
    from apps.agent_definitions import git_http

    return (git_http.PUSH_REFUSED + "\n").encode()


def test_the_dumb_protocol_and_other_paths_are_not_served(w, agent):
    token = pat_token(w["owner"])
    assert Client().get(f"/git/scouts/{SLUG}.git/info/refs", **_basic(token)).status_code == 403
    assert Client().get(f"/git/scouts/{SLUG}.git/HEAD", **_basic(token)).status_code == 404
    assert Client().get(f"/git/scouts/{SLUG}.git/objects/info/packs", **_basic(token)).status_code == 404


def test_who_may_read_and_what_they_see(w, agent):
    assert _draft(_browser(w["editor"])).status_code == 201
    draft = _refs(agent)[git.DRAFT]
    # A person with read sees the draft too.
    assert _advertised(_info_refs(_basic(pat_token(w["viewer"]))))[git.DRAFT] == draft
    # The agent's own login reads its own repository, draft included.
    assert git.DRAFT in _advertised(_info_refs(_basic(pat_token(w["bot"]))))
    # A person outside the workspace: as if it did not exist.
    assert _info_refs(_basic(pat_token(w["outsider"]))).status_code == 404
    assert _info_refs(_basic(pat_token(w["viewer"])), ws="elsewhere").status_code == 404
    assert _info_refs(_basic(pat_token(w["viewer"])), slug="nobody").status_code == 404


def test_another_agents_login_reads_nothing_of_this_one(w, agent):
    other_login = a_member(w["ws"], email="other@agents.test", role=WorkspaceMembership.EDITOR)
    Agent.objects.create(slug="other", name="Other", workspace=w["ws"], owner=w["owner"], user=other_login)
    assert _info_refs(_basic(pat_token(other_login))).status_code == 404


def test_a_runner_reads_main_and_tags_only_and_only_for_an_agent_its_box_may_hold(w, agent):
    assert _draft(_browser(w["editor"])).status_code == 201
    box, _ = _runner_client(w["owner"], w["ws"], agent)
    raw = box.defaults["HTTP_AUTHORIZATION"].split(" ", 1)[1]
    for headers in (_basic(raw), _bearer(raw)):  # Bearer passes the runner-token middleware too
        r = _info_refs(headers)
        assert r.status_code == 200, r.content
        assert set(_advertised(r)) == {"HEAD", git.MAIN, git.tag_ref(1)}
    # A box whose owner does not administer the agent: nothing.
    other, _ = _runner_client(w["editor"], w["ws"], agent)
    assert _info_refs(_basic(other.defaults["HTTP_AUTHORIZATION"].split(" ", 1)[1])).status_code == 404
    # A computer (no runner app): nothing.
    computer, runner = _runner_client(w["owner"], w["ws"], agent)
    Runner.objects.filter(pk=runner.pk).update(kind=Runner.COMPUTER)
    assert _info_refs(_basic(computer.defaults["HTTP_AUTHORIZATION"].split(" ", 1)[1])).status_code == 404


def test_contact_and_caller_tokens_are_refused_like_no_token(w, agent):
    from apps.contacts.models import Contact
    from apps.tokens.models import AppCredential, ContactToken

    app = AppCredential.create_credential(name="site", created_by=w["owner"], workspace=w["ws"])
    visitor = Contact.objects.create(workspace=w["ws"], app=app, external_id="v-1", display_name="V",
                                     source=Contact.SOURCE_EMBED)
    contact_raw, _ = ContactToken.issue(app=app, contact=visitor, ttl_seconds=600)
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key=uuid.uuid4().hex, prompt="hi")
    confined = caller_tokens.mint(turn)
    scoped = caller_tokens.mint(turn, scoped=True)
    for raw in (contact_raw, confined, scoped):
        r = _info_refs(_basic(raw))
        assert r.status_code == 401, raw[:5]
        assert r["WWW-Authenticate"] == 'Basic realm="canopy"'
    for raw in (contact_raw, confined):
        assert _info_refs(_bearer(raw)).status_code in (401, 403)


def test_the_token_never_reaches_the_logs(w, agent, caplog):
    token = pat_token(w["viewer"])
    with caplog.at_level(logging.DEBUG):
        assert _info_refs(_basic(token)).status_code == 200
        assert _info_refs(_basic(token), slug="nobody").status_code == 404
        assert _info_refs(_basic(token + "x")).status_code == 401
        _info_refs(_basic(token), service="git-receive-pack")
    assert token not in caplog.text
    assert base64.b64encode(f"x:{token}".encode()).decode() not in caplog.text


# --- convert with history -------------------------------------------------------


def _git(*args, cwd):
    return subprocess.run(["git", "-c", "user.email=r@r", "-c", "user.name=Ranger", *args], cwd=cwd, check=True,
                          capture_output=True).stdout


def _repo_with_history(tmp_path, *, leak: str = "") -> tuple[bytes, list[str]]:
    src = tmp_path / "ranger"
    src.mkdir()
    _git("init", "-q", "-b", "main", cwd=src)
    (src / "CLAUDE.md").write_text("# Ranger v1\n")
    if leak:
        (src / "notes.txt").write_text(f"token: {leak}\n")
    _git("add", "-A", cwd=src)
    _git("commit", "-qm", "first", cwd=src)
    if leak:
        (src / "notes.txt").unlink()
    (src / "CLAUDE.md").write_text("# Ranger v2\n")
    (src / "skills" / "patrol").mkdir(parents=True)
    (src / "skills" / "patrol" / "SKILL.md").write_text("---\nname: patrol\n---\nGo.\n")
    _git("add", "-A", cwd=src)
    _git("commit", "-qm", "second", cwd=src)
    shas = _git("rev-list", "main", cwd=src).decode().split()
    return _git("bundle", "create", "-", "main", cwd=src), shas


def _convert(client, files, bundle=None):
    body = {"files": files, "message": "move to canopy"}
    if bundle is not None:
        body["bundle_b64"] = base64.b64encode(bundle).decode()
    return _post(client, "/api/agents/ranger/definition/convert", body)


def test_convert_with_a_bundle_keeps_the_history(w, repo_agent, tmp_path):
    bundle, shas = _repo_with_history(tmp_path)
    files = _files(**{"CLAUDE.md": "# Ranger v2\n", "skills/patrol/SKILL.md": "---\nname: patrol\n---\nGo.\n"})
    r = _convert(_browser(w["owner"]), files, bundle)
    assert r.status_code == 201, r.content
    repo_agent.refresh_from_db()
    rev = repo_agent.live_revision
    refs = _refs(repo_agent)
    assert refs[git.IMPORTED] == shas[0] and refs[git.MAIN] == rev.commit
    assert _commit(repo_agent, rev.commit).parents == [shas[0].encode()]
    assert _commit(repo_agent, shas[1]).message == b"first\n"
    # People see the imported history; a box does not need it.
    assert git.IMPORTED in _advertised(_info_refs(_basic(pat_token(w["viewer"])), slug="ranger"))


def test_a_secret_anywhere_in_the_history_refuses_the_convert(w, repo_agent, tmp_path):
    leak = "ghp_" + "A1b2C3d4" * 5
    bundle, shas = _repo_with_history(tmp_path, leak=leak)
    r = _convert(_browser(w["owner"]), _files(**{"CLAUDE.md": "# Ranger v2\n"}), bundle)
    assert r.status_code == 422 and r.json()["error"] == "secret"
    assert r.json()["problems"][0]["path"] == f"notes.txt @ {shas[1][:12]}"
    assert leak not in r.content.decode()
    repo_agent.refresh_from_db()
    assert repo_agent.source == "repo" and not AgentGitRef.objects.filter(agent=repo_agent).exists()
    assert not repo_agent.git_objects.exists()


def test_a_bundle_that_is_not_one_or_is_partial_is_refused(w, repo_agent, tmp_path):
    files = _files(**{"CLAUDE.md": "x\n"})
    assert _convert(_browser(w["owner"]), files, b"not a bundle").json()["error"] == "bad_bundle"
    _bundle, shas = _repo_with_history(tmp_path)
    partial = _git("bundle", "create", "-", f"{shas[1]}..main", cwd=tmp_path / "ranger")
    r = _convert(_browser(w["owner"]), files, partial)
    assert r.status_code == 422 and "partial" in r.json()["title"]


def test_too_much_history_is_413_and_says_convert_the_files(w, repo_agent, monkeypatch, tmp_path):
    bundle, _ = _repo_with_history(tmp_path)
    monkeypatch.setattr(services, "MAX_CONVERT_BYTES", len(bundle))
    r = _convert(_browser(w["owner"]), _files(**{"CLAUDE.md": "x\n"}), bundle)
    assert r.status_code == 413 and "without --with-history" in r.json()["title"]


# --- export and detach --------------------------------------------------------------


@pytest.fixture
def github(tmp_path, settings):
    """A stand-in for github.com: `<base><owner>/<name>.git` is a bare repository on disk."""
    base = tmp_path / "github"
    base.mkdir()
    settings.CANOPY_GITHUB_GIT_BASE = f"{base}/"

    def make(name: str, *, with_commit: bool = False):
        path = base / f"{name}.git"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True)
        if with_commit:
            work = tmp_path / f"seed-{uuid.uuid4().hex[:6]}"
            subprocess.run(["git", "clone", "-q", str(path), str(work)], check=True, capture_output=True)
            (work / "README.md").write_text("hello\n")
            _git("add", "-A", cwd=work)
            _git("commit", "-qm", "init", cwd=work)
            _git("push", "-q", "origin", "main", cwd=work)
        return path

    return make


def _export(client, repo="olive/scout", token="ghs_" + "x" * 36, **extra):
    return _post(client, f"/api/agents/{SLUG}/definition/export", {"repo": repo, "github_token": token}, **extra)


def _published_twice(w):
    assert _draft(_browser(w["editor"])).status_code == 201
    assert _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/revisions/2/publish").status_code == 200


def test_export_pushes_main_history_and_tags_then_the_agent_runs_from_github(w, agent, github, caplog):
    _published_twice(w)
    assert _draft(_browser(w["editor"]), base=2).status_code == 201  # a pending draft is discarded
    bare = github("olive/scout")
    agent.refresh_from_db()
    live = agent.live_revision
    token = "ghs_" + "S3cr3t" * 6
    with caplog.at_level(logging.DEBUG):
        r = _export(pat_client(w["owner"]), token=token)
    assert r.status_code == 200, r.content
    out = r.json()
    assert out["commit"] == live.commit and out["revision"] == 2 and out["tags"] == ["refs/tags/r1", "refs/tags/r2"]
    log = subprocess.run(["git", "--git-dir", str(bare), "log", "--format=%H", "main"], check=True,
                         capture_output=True, text=True).stdout.split()
    one = AgentDefinitionRevision.objects.get(agent=agent, number=1)
    assert log == [live.commit, one.commit]
    tags = subprocess.run(["git", "--git-dir", str(bare), "tag"], check=True, capture_output=True,
                          text=True).stdout.split()
    assert tags == ["r1", "r2"]
    agent.refresh_from_db()
    assert agent.source == "repo" and agent.repo_url == "https://github.com/olive/scout"
    assert agent.repo_ref == "main" and agent.live_revision is None and agent.definition_exported_to == "olive/scout"
    assert AgentDefinitionRevision.objects.get(agent=agent, number=3).status == "discarded"
    # The token went into the push and nowhere else.
    assert token not in caplog.text
    from apps.events.models import SettingChange

    assert token not in json.dumps(list(SettingChange.objects.values("changes", "summary")), default=str)

    # canopy's repository is an archive: still readable, never written.
    assert _info_refs(_basic(pat_token(w["viewer"]))).status_code == 200
    for refused in (_draft(_browser(w["editor"]), base=2),
                    _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/revisions/3/publish"),
                    _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/rollback", {"revision": 1}),
                    _export(pat_client(w["owner"])),
                    _post(pat_client(w["owner"]), f"/api/agents/{SLUG}/definition/detach",
                          {"repo_url": "https://github.com/olive/scout"})):
        assert refused.status_code == 409 and refused.json()["error"] == "exported", refused.content
    d = _browser(w["viewer"]).get(f"/api/agents/{SLUG}/definition").json()
    assert d["exported_to"] == "olive/scout" and d["git_url"] and not d["can_draft"]
    with pytest.raises(services.Refused) as exc:
        services.create_draft(agent, base=2, changes=[{"path": "CLAUDE.md", "content": b"x"}], message="",
                              by=w["owner"], by_agent=False)
    assert exc.value.code == "exported"


def test_export_refuses_a_repository_that_is_not_empty(w, agent, github):
    bare = github("olive/scout", with_commit=True)
    before = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "main"], check=True,
                            capture_output=True, text=True).stdout
    r = _export(pat_client(w["owner"]))
    assert r.status_code == 409 and r.json()["error"] == "not_empty"
    assert subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "main"], check=True, capture_output=True,
                          text=True).stdout == before
    agent.refresh_from_db()
    assert agent.is_stored and not agent.definition_exported_to


def test_export_to_a_repository_that_does_not_exist_says_create_it(w, agent, github):
    r = _export(pat_client(w["owner"]), repo="olive/missing")
    assert r.status_code == 502 and r.json()["error"] == "unreachable"
    agent.refresh_from_db()
    assert agent.is_stored


def test_export_and_detach_are_a_persons_act_by_an_admin(w, agent, github):
    github("olive/scout")
    detach = {"repo_url": "https://github.com/olive/scout"}
    # The agent's own login, even made an admin, and any turn's token: refused.
    AgentAdmin.objects.get_or_create(agent=agent, user=w["bot"], defaults={"granted_by": w["owner"]})
    assert _export(pat_client(w["bot"])).status_code == 403
    assert _post(pat_client(w["bot"]), f"/api/agents/{SLUG}/definition/detach", detach).status_code == 403
    assert _export(pat_client(w["owner"]), HTTP_X_CANOPY_TURN_ID=str(uuid.uuid4())).status_code == 403
    assert _post(pat_client(w["owner"]), f"/api/agents/{SLUG}/definition/detach", detach,
                 HTTP_X_CANOPY_TURN_ID=str(uuid.uuid4())).status_code == 403
    # A person who may edit but does not administer it: refused.
    assert _export(pat_client(w["editor"])).status_code == 403
    assert _post(pat_client(w["editor"]), f"/api/agents/{SLUG}/definition/detach", detach).status_code == 403
    agent.refresh_from_db()
    assert agent.is_stored and not agent.definition_exported_to


def test_detach_switches_without_pushing(w, agent, github):
    bare = github("olive/scout")
    r = _post(pat_client(w["admin"]), f"/api/agents/{SLUG}/definition/detach",
              {"repo_url": "git@github.com:olive/scout.git"})
    assert r.status_code == 200, r.content
    assert r.json()["repo"] == "olive/scout" and r.json()["tags"] == []
    agent.refresh_from_db()
    assert agent.source == "repo" and agent.repo_url == "https://github.com/olive/scout"
    assert agent.definition_exported_to == "olive/scout"
    assert subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "--verify", "-q", "main"],
                          capture_output=True).returncode != 0  # nothing was pushed
    assert _post(pat_client(w["admin"]), f"/api/agents/{SLUG}/definition/detach",
                 {"repo_url": "not a repo"}).status_code == 409  # exported already


def test_detach_refuses_what_is_not_a_github_repository(w, agent):
    r = _post(pat_client(w["owner"]), f"/api/agents/{SLUG}/definition/detach", {"repo_url": "https://example.org/x"})
    assert r.status_code == 422 and r.json()["error"] == "bad_repo"


def test_under_asgi_the_pack_is_streamed_as_it_is_written(w, agent):
    """Behind uvicorn the response is an ASYNC iterator, so Django hands each chunk on as
    dulwich writes it instead of collecting the whole pack first."""
    import asyncio

    from django.test import AsyncRequestFactory, RequestFactory

    from apps.agent_definitions import git_http

    repo = git.serving_repo(_objects(agent).preload(), _refs(agent))
    kind = "application/x-git-upload-pack-advertisement"
    asgi = git_http._stream(AsyncRequestFactory().get("/"), repo, b"", advertise=True, content_type=kind)
    wsgi = git_http._stream(RequestFactory().get("/"), repo, b"", advertise=True, content_type=kind)
    assert asgi.is_async and not wsgi.is_async

    async def collect():
        return [chunk async for chunk in asgi.streaming_content]

    chunks = asyncio.run(collect())
    assert len(chunks) > 1  # one write per pkt-line, not one buffered body
    assert b"".join(chunks) == b"".join(wsgi.streaming_content)
    assert git.MAIN.encode() in b"".join(chunks)


def test_export_without_a_token_says_why_canopy_cannot_use_the_delegation(w, agent, github):
    github("olive/scout")
    r = _post(pat_client(w["owner"]), f"/api/agents/{SLUG}/definition/export", {"repo": "olive/scout", "ref": "main"})
    assert r.status_code == 422 and r.json()["error"] == "no_token" and "--push-myself" in r.json()["title"]
    agent.refresh_from_db()
    assert agent.is_stored
