"""Every stored-agent revision becomes a commit before the blob table is dropped (agents
0138). Delete once that migration has deployed."""
from __future__ import annotations

import hashlib
import json
import zlib

import pytest
from canopy_sdk import agent_definition as sdk
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("agents", "0137_stored_agent_git")]
AFTER = [("agents", "0138_stored_definitions_to_git")]


def _state(targets):
    MigrationExecutor(connection).migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


@pytest.fixture(autouse=True)
def back_to_the_top():
    yield
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def _tree(Blob, files: dict[str, tuple[bytes, bool]]) -> dict:
    tree = {}
    for path, (data, executable) in files.items():
        sha = hashlib.sha256(data).hexdigest()
        Blob.objects.get_or_create(sha256=sha, defaults={"size": len(data), "local": data})
        tree[path] = {"sha256": sha, "size": len(data), "executable": executable}
    return tree


def test_revisions_become_parented_commits_with_their_refs(default_workspace):
    apps = _state(BEFORE)
    Agent = apps.get_model("agents", "Agent")
    Revision = apps.get_model("agents", "AgentDefinitionRevision")
    Blob = apps.get_model("agents", "AgentDefinitionBlob")
    olive = get_user_model().objects.create_user("olive", "olive@dimagi.com", "pw", first_name="Olive")
    agent = Agent.objects.create(slug="scout", name="Scout", workspace_id=default_workspace.pk, source="stored",
                                 repo_url="")
    one_files = {"CLAUDE.md": (b"one\n", False), "bin/scout-email": (b"#!/bin/sh\n", True)}
    two_files = {"CLAUDE.md": (b"two\n", False), "bin/scout-email": (b"#!/bin/sh\n", True)}
    now = timezone.now()
    trees = [_tree(Blob, f) for f in (one_files, two_files, {**two_files, "README.md": (b"draft\n", False)})]
    one = Revision.objects.create(agent=agent, number=1, tree=trees[0], root_sha256=sdk.manifest_root(trees[0]),
                                  status="superseded", author_user_id=olive.pk, published_at=now, message="first")
    two = Revision.objects.create(agent=agent, number=2, parent=one, tree=trees[1],
                                  root_sha256=sdk.manifest_root(trees[1]), status="published", by_agent=True,
                                  published_at=now, message="second")
    Revision.objects.create(agent=agent, number=3, parent=two, tree=trees[2],
                            root_sha256=sdk.manifest_root(trees[2]), status="draft", message="third")
    Agent.objects.filter(pk=agent.pk).update(live_revision=two)

    apps = _state(AFTER)
    Revision = apps.get_model("agents", "AgentDefinitionRevision")
    Ref = apps.get_model("agents", "AgentGitRef")
    Obj = apps.get_model("agents", "AgentGitObject")
    commits = dict(Revision.objects.filter(agent_id=agent.pk).values_list("number", "commit"))
    assert all(len(c) == 40 for c in commits.values())
    refs = dict(Ref.objects.filter(agent_id=agent.pk).values_list("name", "sha"))
    assert refs == {"refs/heads/main": commits[2], "refs/tags/r1": commits[1], "refs/tags/r2": commits[2],
                    "refs/heads/draft": commits[3]}

    def commit(sha):
        row = Obj.objects.get(agent_id=agent.pk, sha=sha)
        return zlib.decompress(bytes(row.data)).decode()

    assert f"parent {commits[1]}" in commit(commits[2]) and f"parent {commits[2]}" in commit(commits[3])
    assert "parent" not in commit(commits[1])
    assert "author Olive <olive@dimagi.com>" in commit(commits[1])
    assert "author scout <agent@canopy.dimagi.com>" in commit(commits[2])
    # The files are the revision's, byte for byte.
    from apps.agents import definition_git as git

    files = git.files_at(git.AgentObjectStore(agent.pk, model=Obj), commits[2])
    assert files == two_files
    assert json.dumps(git.manifest(files), sort_keys=True) == json.dumps(trees[1], sort_keys=True)
