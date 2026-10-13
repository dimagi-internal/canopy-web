"""mail/0002 snapshots every thread's address and drops duplicate SES events before
the (ses_message_id, kind) constraint lands."""
import datetime as dt

import pytest


@pytest.mark.django_db(transaction=True)
def test_0002_snapshots_addresses_and_dedupes_events():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = [("mail", "0001_initial")]
    after = [("mail", "0002_thread_outlives_route")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    user = old.get_model("auth", "User").objects.create(username="u")
    ws = old.get_model("workspaces", "Workspace").objects.create(slug="w", display_name="W", created_by=user)
    d = old.get_model("mail", "MailDomain").objects.create(workspace=ws, name="marshellis.com")
    r = old.get_model("mail", "MailRoute").objects.create(domain=d, local_part="ada", kind="drop")
    t = old.get_model("mail", "MailThread").objects.create(workspace=ws, route=r)
    E = old.get_model("mail", "MailEvent")
    now = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    for kind, mid in [("Bounce", "m1"), ("Bounce", "m1"), ("Delivery", "m1"), ("Send", ""), ("Send", "")]:
        E.objects.create(domain=d, kind=kind, ses_message_id=mid, at=now)

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(after)
    new = executor.loader.project_state(after).apps
    assert new.get_model("mail", "MailThread").objects.get(pk=t.pk).address == "ada@marshellis.com"
    kinds = sorted(new.get_model("mail", "MailEvent").objects.values_list("kind", "ses_message_id"))
    assert kinds == [("Bounce", "m1"), ("Delivery", "m1"), ("Send", ""), ("Send", "")]

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())
