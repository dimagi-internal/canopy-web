"""mail/0002 snapshots every thread's address and agent, and reverses cleanly even
after a route was removed."""
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
    agent = old.get_model("agents", "Agent").objects.create(slug="mada", name="Ada", workspace=ws)
    r = old.get_model("mail", "MailRoute").objects.create(domain=d, local_part="ada", kind="agent",
                                                          agent=agent)
    t = old.get_model("mail", "MailThread").objects.create(workspace=ws, route=r)

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(after)
    new = executor.loader.project_state(after).apps
    got = new.get_model("mail", "MailThread").objects.get(pk=t.pk)
    assert (got.address, got.agent_slug) == ("ada@marshellis.com", "mada")

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_mail_reverses_to_zero_with_a_routeless_thread():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    head = [("mail", "0002_thread_outlives_route")]
    executor = MigrationExecutor(connection)
    executor.migrate(head)
    apps = executor.loader.project_state(head).apps
    user = apps.get_model("auth", "User").objects.create(username="u")
    ws = apps.get_model("workspaces", "Workspace").objects.create(slug="w", display_name="W", created_by=user)
    d = apps.get_model("mail", "MailDomain").objects.create(workspace=ws, name="marshellis.com")
    r = apps.get_model("mail", "MailRoute").objects.create(domain=d, local_part="ada", kind="drop")
    T = apps.get_model("mail", "MailThread")
    T.objects.create(workspace=ws, route=r, address="ada@marshellis.com")
    T.objects.create(workspace=ws, route=None, address="gone@marshellis.com")

    for target in ([("mail", "0001_initial")], [("mail", None)]):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(target)  # must not fail on the NULL route

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())
