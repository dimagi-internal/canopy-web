"""harness 0125 + workspaces 0015/0016: every system account becomes the inbound-mail
triggers it amounted to, then its user and the models go. Delete this file once a
deploy includes them (CLAUDE.md § Migrations)."""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("harness", "0124_agent_triggers"), ("workspaces", "0014_drop_shared_1password_vault")]
AFTER = [("workspaces", "0016_retire_system_accounts")]


def _at(targets):
    """Migrate to `targets`; the historical apps with every OTHER app at its newest
    migration, so their models match their tables."""
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    executor.loader.build_graph()
    named = {app for app, _ in targets} | {"harness", "workspaces"}
    leaves = executor.loader.graph.leaf_nodes()
    nodes = [n for n in leaves if n[0] not in named] + targets
    if not any(app == "harness" for app, _ in targets):
        nodes.append(("harness", "0125_system_accounts_to_triggers"))
    return executor.loader.project_state(nodes).apps


def _leaf():
    MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


def _world(apps):
    User = apps.get_model("auth", "User")
    Workspace = apps.get_model("workspaces", "Workspace")
    Membership = apps.get_model("workspaces", "WorkspaceMembership")
    Agent = apps.get_model("agents", "Agent")
    SystemAccount = apps.get_model("workspaces", "SystemAccount")
    SystemSender = apps.get_model("workspaces", "SystemSender")
    owner = User.objects.create(username="owner", email="owner@dimagi.com")
    ada = User.objects.create(username="ada", email="ada@dimagi-ai.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    Membership.objects.create(workspace=ws, user=owner, role="owner")
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws)
    sys_user = User.objects.create(username="system:connect:aws", email="aws@system.canopy.invalid")
    Membership.objects.create(workspace=ws, user=sys_user, role="editor")
    account = SystemAccount.objects.create(user=sys_user, workspace=ws, name="AWS CloudWatch alarms",
                                           created_by=ada)
    SystemSender.objects.create(account=account, address="no-reply@sns.amazonaws.com",
                                subject_pattern='^(ALARM|OK): "')
    return {"hal": hal, "sys_user": sys_user, "ada": ada, "account": account}


def test_a_system_account_becomes_a_trigger_on_the_agent_its_mail_woke():
    apps = _at(BEFORE)
    x = _world(apps)
    Turn = apps.get_model("harness", "Turn")
    turn = Turn.objects.create(agent=x["hal"], origin="email", idempotency_key="email-1",
                               idempotency_scope="a:hal", initiator_kind="user",
                               initiator_via="email", initiator_assurance="dkim",
                               initiator_user=x["sys_user"],
                               origin_ref={"from": "no-reply@sns.amazonaws.com",
                                           "subject": 'ALARM: "labs-cpu"'})
    try:
        apps = _at(AFTER)
        trigger = apps.get_model("harness", "AgentSchedule").objects.get(kind="inbound_mail")
        assert (trigger.agent_id, trigger.name, trigger.sender_address, trigger.subject_pattern,
                trigger.turn_mode, trigger.created_by_id, trigger.enabled) == (
            x["hal"].pk, "AWS CloudWatch alarms", "no-reply@sns.amazonaws.com", '^(ALARM|OK): "',
            "manual", x["ada"].pk, True)
        turn = apps.get_model("harness", "Turn").objects.get(pk=turn.pk)
        assert (turn.initiator_kind, turn.initiator_via, turn.initiator_user_id) == (
            "system", f"trigger:{trigger.pk}", x["ada"].pk)
        assert turn.origin_ref["trigger_id"] == trigger.pk
        assert not apps.get_model("auth", "User").objects.filter(pk=x["sys_user"].pk).exists()
        assert not apps.get_model("workspaces", "WorkspaceMembership").objects.filter(
            user_id=x["sys_user"].pk).exists()
    finally:
        _leaf()


def test_an_account_whose_agent_cannot_be_told_stops_the_migration():
    apps = _at(BEFORE)
    _world(apps)
    try:
        with pytest.raises(RuntimeError, match="AWS CloudWatch alarms"):
            _at(AFTER)
    finally:
        apps.get_model("workspaces", "SystemAccount").objects.all().delete()
        _leaf()
