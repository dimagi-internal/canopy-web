"""Who may open a connection: the one rule, `recipients(conn)`.

It can only NARROW what the owner's signed statement allows: an agent counts
while its grant is in force (still in the app's workspace and, for a person's
connection, still administered by them); a person's connection reaches the
cloud boxes that run each such agent and their own computers they listed —
never another admin's laptop; an agent's reaches the machines that run it. A
recipient that stops qualifying is pruned from the envelopes without opening
them, except the last machine holding it (a revoke needs one).
"""
from __future__ import annotations

import hashlib

import pytest

from apps.agents.credentials import zk_seal
from apps.agents.models import Agent
from apps.connections import recipients as rcpt
from apps.connections.models import ConnectionGrant
from apps.passkeys.models import UserVaultKeyset
from apps.workspaces.models import Workspace
from tests.fixtures.connections import ShareWorld, revoke_admin, vault_key

pytestmark = pytest.mark.django_db


@pytest.fixture
def world():
    return ShareWorld()


def jkts(rs) -> set[str]:
    return {t for kind, t, _ in rs if kind == "device"}


def test_agent_owned_recipients_are_its_machines_and_the_connecting_admin(world):
    conn = world.shared(world.evas(), agents=[world.eva])
    got = rcpt.recipients(conn)
    assert ("member", str(world.person.pk)) in rcpt.targets(got)
    # The machines that run eva and whose owners may hold it — an admin's laptop runs her turns.
    assert jkts(got) == {world.eva_box.ecdh_jkt, world.ada_laptop.ecdh_jkt}
    assert {t for kind, t, _ in got if kind == "member"} == {str(world.person.pk)}


def test_person_owned_lent_to_two_agents_reaches_both_agents_cloud_boxes(world):
    conn = world.shared(world.persons(), agents=[world.eva, world.echo])
    assert [a.slug for a in rcpt.grants_in_force(conn)] == ["eva", "echo"]
    assert jkts(rcpt.recipients(conn)) == {world.eva_box.ecdh_jkt, world.echo_box.ecdh_jkt}


def test_my_computers_admits_the_persons_computer_runner_only_when_listed(world):
    conn = world.persons()
    unlisted = {"agents": [], "my_computers": []}
    assert jkts(rcpt.recipients(conn, unlisted)) == set()
    listed = {"agents": [], "my_computers": [world.computer.ecdh_jkt, world.ada_computer.ecdh_jkt]}
    # Their own computer, never someone else's however it is listed.
    assert jkts(rcpt.recipients(conn, listed)) == {world.computer.ecdh_jkt}


def test_another_admins_laptop_is_never_a_recipient(world):
    conn = world.persons()
    st = {"agents": [str(world.eva.pk)], "my_computers": [world.ada_laptop.ecdh_jkt]}
    assert world.ada_laptop.ecdh_jkt not in jkts(rcpt.recipients(conn, st))
    assert jkts(rcpt.recipients(conn, st)) == {world.eva_box.ecdh_jkt}


def test_grant_out_of_force_narrows_recipients(world):
    """Review Focus 3: the statement still lists echo; jj no longer administers it."""
    conn = world.shared(world.persons(), agents=[world.eva, world.echo])
    revoke_admin(world.person, world.echo)
    conn.refresh_from_db()
    assert str(world.echo.pk) in conn.statement["agents"]
    assert [a.slug for a in rcpt.grants_in_force(conn)] == ["eva"]
    assert world.echo_box.ecdh_jkt not in {t for _, t, _ in rcpt.recipients(conn)}
    assert world.eva_box.ecdh_jkt in {t for _, t, _ in rcpt.recipients(conn)}


def test_a_statement_agent_without_its_grant_row_is_not_in_force(world):
    conn = world.shared(world.persons(), agents=[world.eva, world.echo])
    ConnectionGrant.objects.filter(connection=conn, agent=world.echo).delete()
    assert [a.slug for a in rcpt.grants_in_force(conn)] == ["eva"]


def test_an_agent_moved_out_of_the_apps_workspace_is_not_in_force(world):
    conn = world.shared(world.persons(), agents=[world.eva, world.echo])
    elsewhere = Workspace.objects.create(slug="x", display_name="X", created_by=world.person)
    Agent.objects.filter(pk=world.echo.pk).update(workspace=elsewhere)
    assert [a.slug for a in rcpt.grants_in_force(conn)] == ["eva"]


def test_owner_member_is_gone_once_the_signer_changes_their_vault_key(world):
    conn = world.persons()
    assert rcpt.owner_member(conn)[:2] == ("member", str(world.person.pk))
    vault_key(world.person)
    assert rcpt.owner_member(conn) is None


def test_holders_and_waiting(world):
    conn = world.shared(world.persons(), agents=[world.eva, world.echo],
                        to=[("device", world.eva_box.ecdh_jkt, world.eva_box.ecdh_public_jwk)])
    assert [k.runner.name for k in rcpt.holders(conn)] == ["eva-cloud"]
    assert rcpt.waiting(conn) == [{"agent": "echo", "runner": "echo-cloud", "target": world.echo_box.ecdh_jkt}]


def test_prune_drops_a_machine_that_no_longer_qualifies_from_both_envelopes(world):
    conn = world.shared(world.persons(), agents=[world.eva, world.echo])
    revoke_admin(world.person, world.echo)
    assert rcpt.prune(conn) == ["echo-cloud"]
    conn.refresh_from_db()
    want = {("member", str(world.person.pk)), ("device", world.eva_box.ecdh_jkt)}
    assert zk_seal.sealed_targets(conn.sealed) == want
    assert {(m["kind"], m["target"]) for m in conn.sealed_sig["macs"]} == want
    assert conn.sealed_digest == hashlib.sha256(conn.sealed.encode()).hexdigest()
    assert rcpt.prune(conn) == []


def test_prune_never_drops_the_last_device_holder(world):
    conn = world.shared(world.persons(), agents=[world.echo])
    revoke_admin(world.person, world.echo)
    assert rcpt.recipients(conn) == [rcpt.owner_member(conn)]
    assert rcpt.prune(conn) == []
    conn.refresh_from_db()
    # Kept so a revoke still has a machine that can open it; the material route refuses it.
    assert ("device", world.echo_box.ecdh_jkt) in zk_seal.sealed_targets(conn.sealed)


def test_prune_leaves_an_unshared_connection_alone(world):
    conn = world.persons()
    UserVaultKeyset.objects.filter(user=world.person).delete()
    before = conn.sealed
    assert rcpt.prune(conn) == []
    conn.refresh_from_db()
    assert conn.sealed == before
