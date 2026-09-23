"""0030 removes only what the socket's old auto-join wrote, and nothing a real
grant made."""
import datetime as dt

import pytest


@pytest.mark.django_db(transaction=True)
def test_the_purge_keeps_owners_slack_and_real_shares():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = [("canopy_sessions", "0029_binding_missed_reports")]
    after = [("canopy_sessions", "0030_purge_auto_join_participants")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    User = old.get_model("auth", "User")
    Session = old.get_model("canopy_sessions", "Session")
    P = old.get_model("canopy_sessions", "SessionParticipant")
    creator, mate, slacker, later = (User.objects.create(username=n) for n in ("c", "m", "s", "l"))
    ws = old.get_model("workspaces", "Workspace").objects.create(slug="w", display_name="W",
                                                                 created_by=creator)
    chat = Session.objects.create(workspace=ws, created_by=creator, origin="web")
    thread = Session.objects.create(workspace=ws, created_by=creator, origin="web",
                                    metadata={"slack_channel": "C1", "slack_thread": "k"})
    early = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    for session, user, role in [(chat, creator, "owner"), (chat, mate, "editor"),
                                (thread, slacker, "editor"), (chat, later, "editor")]:
        P.objects.create(session=session, user=user, role=role)
    P.objects.exclude(user=later).update(created_at=early)   # `later` is a post-cutoff share

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(after)
    new = executor.loader.project_state(after).apps
    left = set(new.get_model("canopy_sessions", "SessionParticipant").objects
               .values_list("user__username", flat=True))
    assert left == {"c", "s", "l"}  # the auto-joined co-tenant is the only one gone

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())
