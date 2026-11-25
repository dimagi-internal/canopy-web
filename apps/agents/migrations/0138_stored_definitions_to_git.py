"""Every stored-agent revision becomes a commit in its agent's repository.

For each agent with revisions, in revision order: the revision's files (from its
`AgentDefinitionBlob`s) are written as a commit on its parent revision's commit; a
published or superseded revision is tagged `r<N>`, the live one is `main` and the one
draft is `draft`. Its own migration, ahead of the one that drops the blobs: it writes
rows (`AgentDefinitionRevision.commit`) other tables reference.
"""
from __future__ import annotations

import datetime as dt

from django.db import migrations


def _files(Blob, tree: dict) -> dict[str, tuple[bytes, bool]]:
    from apps.canopy_sessions import attachment_storage

    out = {}
    for path, entry in sorted((tree or {}).items()):
        blob = Blob.objects.get(sha256=entry["sha256"])
        data = attachment_storage.get(blob.storage_key).body if blob.storage_key else bytes(blob.local or b"")
        out[path] = (data, bool(entry.get("executable")))
    return out


def _when(value) -> int | None:
    return int(value.astimezone(dt.UTC).timestamp()) if value else None


def build_commits(apps, schema_editor):
    from apps.agents import definition_git as git

    Agent = apps.get_model("agents", "Agent")
    Revision = apps.get_model("agents", "AgentDefinitionRevision")
    Blob = apps.get_model("agents", "AgentDefinitionBlob")
    GitObject = apps.get_model("agents", "AgentGitObject")
    GitRef = apps.get_model("agents", "AgentGitRef")
    agent_ids = Revision.objects.filter(commit="").values_list("agent_id", flat=True).distinct()
    for agent in Agent.objects.filter(pk__in=list(agent_ids)):
        objects = git.AgentObjectStore(agent.pk, model=GitObject)
        commits: dict[int, str] = {}
        for rev in Revision.objects.filter(agent_id=agent.pk).order_by("number"):
            if rev.commit:
                commits[rev.pk] = rev.commit
                continue
            user = rev.author_user
            if rev.by_agent or user is None:
                author = git.agent_author(agent.slug)
            else:
                name = f"{user.first_name} {user.last_name}".strip() or user.email
                author = git.identity(name, user.email)
            files = _files(Blob, rev.tree)
            rev.commit = git.make_commit(objects, tree=git.write_tree(objects, files),
                                         parents=[commits[rev.parent_id]] if rev.parent_id in commits else [],
                                         author=author, message=rev.message or f"Revision {rev.number}",
                                         when=_when(rev.published_at or rev.created_at))
            objects.flush()
            rev.save(update_fields=["commit"])
            commits[rev.pk] = rev.commit
            if rev.status in ("published", "superseded"):
                git.set_ref(agent.pk, git.tag_ref(rev.number), rev.commit, model=GitRef)
            elif rev.status == "draft":
                git.set_ref(agent.pk, git.DRAFT, rev.commit, model=GitRef)
        if agent.live_revision_id in commits:
            git.set_ref(agent.pk, git.MAIN, commits[agent.live_revision_id], model=GitRef)


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0137_stored_agent_git"),
    ]

    operations = [
        migrations.RunPython(build_commits, migrations.RunPython.noop),
    ]
