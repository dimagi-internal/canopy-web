"""Who may open a connection: the one rule, `recipients(conn)`.

It can only NARROW what the owner's signed statement allows: an agent counts
while its grant is in force (still in the app's workspace and, for a person's
connection, still administered by them); a person's connection reaches the
cloud boxes that run each such agent and their own computers they listed —
never another admin's laptop; an agent's reaches the machines that run it. A
recipient that stops qualifying is pruned from the envelopes without opening
them, except one active cloud box when nothing kept could revoke it (a
revoke needs one) — never a revoked key, a laptop or a computer.
"""
from __future__ import annotations

import hashlib

import pytest

from apps.agents.credentials import zk, zk_seal
from apps.agents.models import Agent, AgentVaultEndorsement
from apps.connections import recipients as rcpt
from apps.connections.models import Connection, ConnectionGrant
from apps.harness import device_keys
from apps.harness.models import Runner, RunnerAssignment, RunnerDeviceKey
from apps.passkeys.models import UserVaultKeyset
from apps.workspaces.models import Workspace
from tests.fixtures.connections import ShareWorld, endorse, revoke_admin, sealed_to_owner, vault_key
from tests.fixtures.vault_zk import ref as zk_ref

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


def test_owner_member_stays_the_key_the_envelope_names_after_the_signer_replaced_it(world):
    conn = world.persons()
    before = rcpt.owner_member(conn)
    assert before[:2] == ("member", str(world.person.pk))
    assert device_keys.thumbprint(before[2]) == conn.owner_key
    vault_key(world.person)
    assert rcpt.owner_member(conn) == before


def test_holders_and_waiting(world):
    conn = world.shared(world.persons(), agents=[world.eva, world.echo],
                        to=[("device", world.eva_box.ecdh_jkt, world.eva_box.ecdh_public_jwk)])
    assert [k.runner.name for k in rcpt.holders(conn)] == ["eva-cloud"]
    assert rcpt.waiting(conn) == [{"agent": "echo", "runner": "echo-cloud", "target": world.echo_box.ecdh_jkt}]


def test_unreachable_names_each_lent_agent_no_machine_can_receive_it_for(world):
    conn = world.shared(world.persons(), agents=[world.eva, world.echo])
    assert rcpt.unreachable(conn) == []
    # Machines run echo, and nobody the statement names vouches for them.
    AgentVaultEndorsement.objects.filter(target=world.echo_box.ecdh_jkt).delete()
    assert rcpt.unreachable(conn) == [{"agent": "echo", "reason": "unvouched"}]
    # Nothing runs it.
    RunnerAssignment.objects.filter(agent=world.echo).delete()
    assert rcpt.unreachable(conn) == [{"agent": "echo", "reason": "no_machine"}]
    # jj no longer administers it.
    revoke_admin(world.person, world.echo)
    assert rcpt.unreachable(conn) == [{"agent": "echo", "reason": "lapsed"}]


def test_unreachable_is_empty_until_shared(world):
    conn = world.persons()
    assert rcpt.unreachable(conn) == [] and conn.state == "unshared"


def test_a_machine_only_waiting_for_its_copy_is_not_unreachable(world):
    conn = world.shared(world.persons(), agents=[world.eva, world.echo],
                        to=[("device", world.eva_box.ecdh_jkt, world.eva_box.ecdh_public_jwk)])
    assert [w["agent"] for w in rcpt.waiting(conn)] == ["echo"] and rcpt.unreachable(conn) == []


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


def test_prune_keeps_the_last_active_cloud_box_holding_it(world):
    conn = world.shared(world.persons(), agents=[world.echo])
    revoke_admin(world.person, world.echo)
    assert rcpt.recipients(conn) == [rcpt.owner_member(conn)]
    assert rcpt.prune(conn) == []
    conn.refresh_from_db()
    # Kept so a revoke still has a machine that can open it; the material route refuses it.
    assert ("device", world.echo_box.ecdh_jkt) in zk_seal.sealed_targets(conn.sealed)


def held(conn) -> set[str]:
    conn.refresh_from_db()
    return {t for kind, t in zk_seal.sealed_targets(conn.sealed) if kind == "device"}


def test_prune_drops_a_revoked_key_even_when_it_is_the_last_holder(world):
    conn = world.shared(world.persons(), agents=[world.echo])
    RunnerDeviceKey.objects.filter(pk=world.echo_box.pk).update(status=RunnerDeviceKey.REVOKED)
    assert rcpt.prune(conn) == ["echo-cloud"]
    assert held(conn) == set()
    assert zk_seal.sealed_targets(conn.sealed) == {("member", str(world.person.pk))}


def test_prune_drops_a_computer_even_when_it_is_the_last_holder(world):
    conn = world.shared(world.persons(), my_computers=[world.computer.ecdh_jkt])
    Connection.objects.filter(pk=conn.pk).update(statement={**conn.statement, "my_computers": []})
    assert rcpt.prune(conn) == ["jj-computer"]
    assert held(conn) == set()


def test_prune_drops_a_laptop_even_when_it_is_the_last_holder(world):
    laptop, _, _ = world._box("jj-laptop", Runner.EMDASH, world.person, world.eva)
    conn = world.shared(world.persons(), agents=[world.eva],
                        to=[("device", laptop.ecdh_jkt, laptop.ecdh_public_jwk)])
    elsewhere = Workspace.objects.create(slug="x", display_name="X", created_by=world.person)
    Agent.objects.filter(pk=world.eva.pk).update(workspace=elsewhere)
    assert rcpt.prune(conn) == ["jj-laptop"]
    assert held(conn) == set()


def test_prune_keeps_an_out_of_force_cloud_box_beside_the_owners_computer(world):
    """The owner's computer never revokes, so the cloud box stays the revoker."""
    conn = world.shared(world.persons(), agents=[world.echo], my_computers=[world.computer.ecdh_jkt])
    revoke_admin(world.person, world.echo)
    assert rcpt.prune(conn) == []
    assert held(conn) == {world.echo_box.ecdh_jkt, world.computer.ecdh_jkt}


def test_prune_leaves_a_revoking_connection_alone(world):
    conn = world.shared(world.persons(), agents=[world.eva, world.echo])
    revoke_admin(world.person, world.echo)
    Connection.objects.filter(pk=conn.pk).update(state=Connection.REVOKING)
    assert rcpt.prune(conn) == []
    assert held(conn) == {world.eva_box.ecdh_jkt, world.echo_box.ecdh_jkt}


# ---- who may vouch for a machine (contract §0.5) ----------------------------------------

def _statement(world, *agents, endorsers=()):
    return {"agents": sorted(str(a.pk) for a in agents), "my_computers": [], "endorsers": sorted(endorsers)}


@pytest.fixture
def vouched(world):
    """A box that runs eva, endorsed by ada alone (an admin of eva, not the owner)."""
    world.box2, _, _ = world._box("eva-cloud-2", Runner.CLOUD, world.op, world.eva, endorsed=False)
    endorse(world.eva, world.box2, world.ada, world.ada_priv)
    world.ada_jkt = zk_ref.jkt(world.ada_priv.public_key())
    return world


def test_a_machine_counts_only_through_the_owner_or_a_listed_endorser(vouched):
    world, conn = vouched, vouched.persons()
    owners_only = _statement(world, world.eva)
    assert jkts(rcpt.recipients(conn, owners_only)) == {world.eva_box.ecdh_jkt}
    listed = _statement(world, world.eva, endorsers=[world.ada_jkt])
    assert jkts(rcpt.recipients(conn, listed)) == {world.eva_box.ecdh_jkt, world.box2.ecdh_jkt}


def test_a_machine_nobody_endorsed_is_not_a_recipient_of_any_statement(vouched):
    world, conn = vouched, vouched.persons()
    AgentVaultEndorsement.objects.filter(target=world.box2.ecdh_jkt).delete()
    listed = _statement(world, world.eva, endorsers=[world.ada_jkt])
    assert world.box2.ecdh_jkt not in jkts(rcpt.recipients(conn, listed))


def test_an_agents_own_connection_is_narrowed_by_the_same_rule(vouched):
    world = vouched
    conn = sealed_to_owner({"agent": world.eva}, world.person, app=world.app)
    assert world.box2.ecdh_jkt not in jkts(rcpt.recipients(conn, _statement(world, world.eva)))
    listed = _statement(world, world.eva, endorsers=[world.ada_jkt])
    assert world.box2.ecdh_jkt in jkts(rcpt.recipients(conn, listed))


def test_the_stored_statements_endorsers_decide_by_default(vouched):
    world = vouched
    conn = world.shared(world.persons(), agents=[world.eva], endorsers=[world.ada_jkt])
    assert world.box2.ecdh_jkt in jkts(rcpt.recipients(conn))
    Connection.objects.filter(pk=conn.pk).update(statement={**conn.statement, "endorsers": []})
    conn.refresh_from_db()
    assert world.box2.ecdh_jkt not in jkts(rcpt.recipients(conn))
    assert rcpt.waiting(conn) == []
    assert rcpt.prune(conn) == ["eva-cloud-2"]


def test_the_endorser_is_who_the_verifying_key_says_not_what_the_row_claims(vouched):
    """A stranger's endorsement stored as if ada made it: a box computes the thumbprint
    from the key that verifies the signature, so it is the stranger's."""
    world, conn = vouched, vouched.persons()
    stranger_priv = vault_key(world.op)
    endorse(world.eva, world.box2, world.op, stranger_priv, endorser_jkt=world.ada_jkt)
    AgentVaultEndorsement.objects.filter(target=world.box2.ecdh_jkt, endorser=world.ada).delete()
    listed = _statement(world, world.eva, endorsers=[world.ada_jkt])
    assert world.box2.ecdh_jkt not in jkts(rcpt.recipients(conn, listed))
    # The stranger, listed by their real key, does count.
    real = _statement(world, world.eva, endorsers=[zk_ref.jkt(stranger_priv.public_key())])
    assert world.box2.ecdh_jkt in jkts(rcpt.recipients(conn, real))


def test_an_endorsement_that_does_not_verify_counts_for_nothing(vouched):
    world, conn = vouched, vouched.persons()
    forged = zk_ref.endorse(world.echo.pk, "device", world.box2.ecdh_jkt, zk.public_key(world.box2.ecdh_public_jwk),
                            world.ada_priv, world.ada.pk)["sig"]  # signed for another agent
    AgentVaultEndorsement.objects.filter(target=world.box2.ecdh_jkt).update(sig=forged)
    listed = _statement(world, world.eva, endorsers=[world.ada_jkt])
    assert world.box2.ecdh_jkt not in jkts(rcpt.recipients(conn, listed))


def test_an_endorser_who_replaced_their_key_vouches_for_nothing(vouched):
    world, conn = vouched, vouched.persons()
    new = vault_key(world.ada)
    for who in (world.ada_jkt, zk_ref.jkt(new.public_key())):
        assert world.box2.ecdh_jkt not in jkts(rcpt.recipients(conn, _statement(world, world.eva, endorsers=[who])))


def test_an_endorsement_for_another_agent_does_not_admit_a_machine(vouched):
    world, conn = vouched, vouched.persons()
    AgentVaultEndorsement.objects.filter(target=world.box2.ecdh_jkt).delete()
    RunnerAssignment.objects.create(agent=world.echo, runner=world.box2.runner, rank=1)
    endorse(world.echo, world.box2, world.person, world.person_priv)
    assert world.box2.ecdh_jkt not in jkts(rcpt.recipients(conn, _statement(world, world.eva)))
    assert world.box2.ecdh_jkt in jkts(rcpt.recipients(conn, _statement(world, world.echo)))


def test_my_computers_need_no_endorsement(world):
    conn = world.persons()
    st = {"agents": [], "my_computers": [world.computer.ecdh_jkt], "endorsers": []}
    assert jkts(rcpt.recipients(conn, st)) == {world.computer.ecdh_jkt}


def test_prune_leaves_an_unshared_connection_alone(world):
    conn = world.persons()
    UserVaultKeyset.objects.filter(user=world.person).delete()
    before = conn.sealed
    assert rcpt.prune(conn) == []
    conn.refresh_from_db()
    assert conn.sealed == before
