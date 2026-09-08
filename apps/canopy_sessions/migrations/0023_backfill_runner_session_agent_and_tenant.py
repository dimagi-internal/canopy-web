"""Re-home runner-reported sessions onto the agent whose project they name.

The wholesale runner sweep carried no agent, so canopy filed every reported
session under the RUNNER's workspace with `agent = NULL`. Every runner is
registered in `dimagi` while ace/ada/echo/hal live in `connect`, so each of
those agents' sessions sat in a tenant that does not contain the agent it is
about — visible to that tenant's members, and invisible to a lister scoped to
the agent's own.

`services._agent_for_project` fixes new rows. This moves the ones already
written, because the tenancy is wrong for them *now* — leaving them would keep
a real cross-tenant exposure in place and leave the feed permanently mixed.

Deliberately narrow:

  * ORIGIN_RUNNER only. A web-created session was tenanted by the person who
    created it, from the URL, and that is authoritative — never overwrite it.
  * `agent IS NULL` only. A session already attributed has an owner from a path
    that knew better than this one does.
  * `project` must match an Agent slug exactly. A real repo checkout that is
    nobody's agent (canopy-web, connect-labs) is correctly the runner's.

Reversible: the backwards pass restores `agent = NULL` but deliberately does NOT
move the workspace back. Which tenant a row came FROM is not recorded, so the
honest inverse cannot restore it — and guessing "the runner's workspace" would
re-file rows that may never have been mis-tenanted. Un-attributing is enough to
undo the join this migration performs.

One behaviour DOES change for callers, and it is the intended one: a reuse
lookup that addresses "ace" as a PROJECT (`resolve_session` accepts an agentless
project — "the phone addresses repos too") will no longer match these rows,
because they are now the ACE AGENT's. That is the correct target after this
migration, and the failure mode if a caller does not follow is a new thread
rather than a wrong one.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Session = apps.get_model("canopy_sessions", "Session")
    Agent = apps.get_model("agents", "Agent")

    by_slug = {a.slug: a for a in Agent.objects.select_related("workspace").all()}
    if not by_slug:
        return

    moved = 0
    rows = Session.objects.filter(origin="runner", agent__isnull=True).exclude(project="")
    for session in rows.iterator():
        owner = by_slug.get(session.project)
        if owner is None or owner.workspace_id is None:
            continue
        session.agent_id = owner.id
        session.workspace_id = owner.workspace_id
        # XOR: chat_session_not_agent_and_project. Clearing `project` loses
        # nothing — `Session.emdash_project` returns the agent's slug instead,
        # so the (project, task) pair a runner resolves a transcript by is
        # unchanged, and RunnerBinding.emdash_project (the key the 10s report
        # loop reuses on) is its own cached column and does not move.
        session.project = ""
        session.save(update_fields=["agent", "workspace", "project"])
        moved += 1
    if moved:
        print(f"  re-homed {moved} runner-reported session(s) onto their agent")


def backwards(apps, schema_editor):
    Session = apps.get_model("canopy_sessions", "Session")
    Agent = apps.get_model("agents", "Agent")
    slugs = set(Agent.objects.values_list("slug", flat=True))
    if not slugs:
        return
    Session.objects.filter(origin="runner", project__in=slugs).update(agent=None)


class Migration(migrations.Migration):
    dependencies = [
        ("canopy_sessions", "0022_runnerbinding_agent_status_stale"),
        ("agents", "0001_initial"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
