"""The owner shares a connection: a signed grant statement and an envelope their
browser re-sealed to the machines it allows (contract §0.3-0.4, route 4).

canopy-web never opens the envelope and never signs. The browser here is the
connections reference (`docs/vault/zk-v1/connections_v1_reference.py`), so the
statement and the writer MACs are the contract's bytes. What canopy-web checks:
the owner's key signed it, the version moves forward, every added agent is one
the owner administers in the app's workspace, listed computers are the owner's
own, the write replaces what was read, and the envelope reaches only the owner
and machines the statement allows, with the owner as its writer.
"""
from __future__ import annotations

import json
import logging

import pytest
from django.test import Client

from apps.agents.credentials import zk, zk_seal
from apps.agents.models import Agent, AgentVaultEndorsement
from apps.connections import recipients as rcpt
from apps.connections import services, sharing
from apps.connections.models import Connection
from apps.events.models import Event
from apps.harness.models import Runner
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import (
    FAKE_REFRESH,
    ShareWorld,
    browser_share,
    endorse,
    refused,
    revoke_admin,
    vault_key,
)
from tests.fixtures.vault_zk import conn_ref
from tests.fixtures.vault_zk import ref as zk_ref

pytestmark = pytest.mark.django_db


@pytest.fixture
def world():
    return ShareWorld()


def log_lines(caplog, verb):
    return [r.getMessage() for r in caplog.records
            if r.name == "apps.connections.services" and r.getMessage().startswith(f"connections.{verb} ")]


def client(user, *, step_up=True):
    c = Client()
    c.force_login(user)
    if step_up:
        grant_step_up(c)
    return c


# ---- sharing -----------------------------------------------------------------------

def test_share_signs_seals_and_activates(world, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    conn = world.persons()
    conn = sharing.share(conn, user=world.person, **browser_share(conn, world.person, world.person_priv,
                                                                   agents=[world.eva, world.echo]))
    conn.refresh_from_db()
    assert conn.state == "active" and conn.statement_version == 1
    assert {("device", world.eva_box.ecdh_jkt), ("member", str(world.person.pk))} <= zk_seal.sealed_targets(conn.sealed)
    assert set(conn.grants.values_list("agent__slug", flat=True)) == {"eva", "echo"}
    assert [line for line in log_lines(caplog, "shared") if "version=1" in line]
    assert len(log_lines(caplog, "granted")) == 2
    # The machine opens what the owner's browser sealed; canopy-web stored only ciphertext.
    opened = zk_ref.open_sealed_value(conn.sealed, conn.binding, "refresh", "device", world.eva_box.ecdh_jkt,
                                      world.eva_ecdh)
    assert FAKE_REFRESH in opened and FAKE_REFRESH not in conn.sealed


def test_resharing_follows_the_statement_and_records_each_change(world):
    conn = world.evas()
    world.shared(conn, agents=[world.eva, world.echo])
    world.shared(conn, agents=[world.eva])
    conn.refresh_from_db()
    assert conn.statement_version == 2 and not conn.grants.exists()
    assert list(Event.objects.order_by("id").values_list("kind", flat=True)) == [
        "connections.granted", "connections.shared", "connections.grant_revoked", "connections.shared"]


def test_share_sealed_to_a_device_the_statement_does_not_allow_is_refused(world):
    conn = world.persons()
    stranger = ("device", world.ada_laptop.ecdh_jkt, world.ada_laptop.ecdh_public_jwk)
    refused(conn, world.person, "foreign_reader",
            **browser_share(conn, world.person, world.person_priv, agents=[world.eva], to=[stranger]))
    assert Connection.objects.get(pk=conn.pk).state == "unshared"


def test_share_without_writer_mac_is_refused(world):
    conn = world.persons()
    body = browser_share(conn, world.person, world.person_priv, agents=[world.eva])
    refused(conn, world.person, "bad_writer_sig", **{**body, "sealed_sig": None})
    # Nor MACs written by anyone but the owner.
    body = browser_share(conn, world.person, world.person_priv, agents=[world.eva], writer=world.ada_priv)
    refused(conn, world.person, "bad_writer_sig", **body)


def test_share_signed_by_another_admin_is_refused(world):
    conn = world.evas()
    refused(conn, world.ada, "not_the_owner_key",
            **browser_share(conn, world.ada, world.ada_priv, agents=[world.eva]))


def test_a_bad_signature_is_refused(world):
    conn = world.persons()
    refused(conn, world.person, "bad_signature",
            **browser_share(conn, world.person, world.person_priv, agents=[world.eva], sign_with=world.ada_priv))


def test_an_older_or_equal_version_is_refused(world):
    conn = world.shared(world.persons(), agents=[world.eva])
    for version in (1, 0):
        body = browser_share(conn, world.person, world.person_priv, agents=[world.eva], version=version)
        refused(conn, world.person, "statement_stale" if version else "bad_statement", **body)


def test_lending_to_an_agent_you_do_not_administer_is_refused(world):
    conn = world.persons()
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=world.ws, owner=world.op)
    refused(conn, world.person, "not_an_admin",
            **browser_share(conn, world.person, world.person_priv, agents=[world.eva, hal], to=[]))


def test_lending_outside_the_apps_workspace_is_refused(world):
    conn = world.persons()
    x = Workspace.objects.create(slug="x", display_name="X", created_by=world.person)
    WorkspaceMembership.objects.create(workspace=x, user=world.person, role=WorkspaceMembership.EDITOR)
    far = Agent.objects.create(slug="far", name="Far", workspace=x, owner=world.person)
    refused(conn, world.person, "agent_outside_workspace",
            **browser_share(conn, world.person, world.person_priv, agents=[far], to=[]))


def test_an_agent_already_lent_may_stay_listed_after_its_lender_stopped_administering_it(world):
    conn = world.shared(world.persons(), agents=[world.eva, world.echo])
    revoke_admin(world.person, world.echo)
    world.shared(conn, agents=[world.eva, world.echo])
    assert Connection.objects.get(pk=conn.pk).statement_version == 2


def test_my_computers_must_be_the_signers_own_machines(world):
    conn = world.persons()
    refused(conn, world.person, "not_your_machine",
            **browser_share(conn, world.person, world.person_priv, my_computers=[world.ada_computer.ecdh_jkt],
                            to=[]))
    ok = world.shared(conn, my_computers=[world.computer.ecdh_jkt])
    assert ("device", world.computer.ecdh_jkt) in zk_seal.sealed_targets(ok.sealed)


def test_an_agents_connection_lists_no_computers(world):
    conn = world.evas()
    refused(conn, world.person, "not_your_machine",
            **browser_share(conn, world.person, world.person_priv, agents=[world.eva],
                            my_computers=[world.computer.ecdh_jkt], to=[]))


def test_agent_owned_statement_must_list_the_owner_agent(world):
    conn = world.evas()
    refused(conn, world.person, "statement_missing_owner_agent",
            **browser_share(conn, world.person, world.person_priv, agents=[world.echo]))


def test_a_moved_agents_connection_cannot_be_shared(world):
    conn = world.evas()
    x = Workspace.objects.create(slug="x", display_name="X", created_by=world.person)
    WorkspaceMembership.objects.create(workspace=x, user=world.person, role=WorkspaceMembership.EDITOR)
    Agent.objects.filter(pk=world.eva.pk).update(workspace=x)
    refused(conn, world.person, "agent_outside_workspace",
            **browser_share(conn, world.person, world.person_priv, agents=[world.eva], to=[]))


def test_a_write_over_a_value_you_did_not_read_is_refused(world):
    conn = world.persons()
    body = browser_share(conn, world.person, world.person_priv, agents=[world.eva])
    refused(conn, world.person, "zk_value_changed", **{**body, "expect": "0" * 64})


def test_a_revoking_connection_is_not_shared(world):
    conn = world.persons()
    body = browser_share(conn, world.person, world.person_priv, agents=[world.eva])
    Connection.objects.filter(pk=conn.pk).update(state=Connection.REVOKING)
    refused(conn, world.person, "revoking", **body)


# ---- routes ------------------------------------------------------------------------

def _put(c, conn, body):
    return c.put(f"/api/connections/{conn.pk}/share", body, content_type="application/json")


def test_share_route_answers_the_connection_active(world):
    conn = world.persons()
    r = _put(client(world.person), conn, browser_share(conn, world.person, world.person_priv, agents=[world.eva]))
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["state"] == "active" and body["statement"]["agents"] == ["eva"]
    assert body["holders"] == ["eva-cloud"] and body["waiting"] == []


def test_share_route_refusals_are_problems(world):
    conn = world.persons()
    body = browser_share(conn, world.person, world.person_priv, agents=[world.eva])
    r = _put(client(world.person), conn, {**body, "expect": "0" * 64})
    assert r.status_code == 409 and r.json()["error"] == "zk_value_changed"


def test_share_needs_a_passkey_step_up(world):
    conn = world.persons()
    r = _put(client(world.person, step_up=False), conn,
             browser_share(conn, world.person, world.person_priv, agents=[world.eva]))
    assert r.status_code == 403 and r.json()["error"] == "step_up_required"
    assert Connection.objects.get(pk=conn.pk).state == "unshared"


def test_share_state_is_the_owners_only(world):
    conn = world.evas()
    mine = client(world.person).get(f"/api/connections/{conn.pk}/share")
    assert mine.status_code == 200, mine.json()
    # Another admin of eva sees it exists but holds no key to share it.
    other = client(world.ada).get(f"/api/connections/{conn.pk}/share")
    assert other.status_code == 403 and other.json()["error"] == "not_the_owner_key"
    # Someone who cannot see it gets a 404.
    nobody = client(world.op)
    nobody_conn = world.persons()
    assert nobody.get(f"/api/connections/{nobody_conn.pk}/share").status_code == 404


def test_share_state_lists_every_machine_the_owner_could_choose(world):
    conn = world.persons()
    body = client(world.person).get(f"/api/connections/{conn.pk}/share").json()
    assert body["binding"] == f"connection:{conn.pk}" and body["sealed_digest"] == conn.sealed_digest
    assert body["owner"]["user_id"] == str(world.person.pk)
    rows = {(c["kind"], c["runner_name"], c["agent_id"], c["mine"]) for c in body["candidates"]}
    assert rows == {("member", "", "", True), ("device", "eva-cloud", str(world.eva.pk), False),
                    ("device", "echo-cloud", str(world.echo.pk), False), ("device", "jj-computer", "", True)}
    assert {a["slug"] for a in body["agents"]} == {"eva", "echo"}


def test_listed_connection_names_my_machines_and_waiting(world):
    conn = world.shared(world.persons(), agents=[world.eva, world.echo], my_computers=[world.computer.ecdh_jkt],
                        to=[("device", world.eva_box.ecdh_jkt, world.eva_box.ecdh_public_jwk)])
    row = next(c for c in client(world.person).get("/api/connections/").json()["connections"]
               if c["id"] == str(conn.pk))
    assert row["waiting"] == [{"agent": "echo", "runner": "echo-cloud"}, {"agent": "", "runner": "jj-computer"}]
    assert [m["name"] for m in row["my_machines"]] == ["jj-computer"]
    assert len(row["my_machines"][0]["fingerprint"].split(" ")) == 8


# ---- who may vouch (contract §0.3, §0.5) ---------------------------------------------

@pytest.fixture
def vouched(world):
    """A box that runs eva, endorsed by ada alone (an admin of eva, not the owner)."""
    world.box2, _, _ = world._box("eva-cloud-2", Runner.CLOUD, world.op, world.eva, endorsed=False)
    endorse(world.eva, world.box2, world.ada, world.ada_priv)
    world.ada_jkt = zk_ref.jkt(world.ada_priv.public_key())
    return world


def _to(key):
    return ("device", key.ecdh_jkt, key.ecdh_public_jwk)


def test_a_share_sealed_to_a_machine_no_listed_person_vouches_for_is_refused(vouched):
    world, conn = vouched, vouched.persons()
    refused(conn, world.person, "foreign_reader", **browser_share(
        conn, world.person, world.person_priv, agents=[world.eva], to=[_to(world.eva_box), _to(world.box2)]))


def test_a_share_listing_the_endorser_reaches_their_machine_and_is_stored(vouched):
    world, conn = vouched, vouched.persons()
    conn = world.shared(conn, agents=[world.eva], endorsers=[world.ada_jkt],
                        to=[_to(world.eva_box), _to(world.box2)])
    conn.refresh_from_db()
    assert conn.statement["endorsers"] == [world.ada_jkt]
    assert ("device", world.box2.ecdh_jkt) in zk_seal.sealed_targets(conn.sealed)
    (row,) = client(world.person).get("/api/connections/").json()["connections"]
    assert row["statement"]["endorsers"] == [world.ada_jkt]


def test_share_state_answers_the_statement_and_its_signature(world):
    conn = world.persons()
    body = client(world.person).get(f"/api/connections/{conn.pk}/share").json()
    assert body["statement"] is None and body["statement_sig"] == ""
    conn = world.shared(conn, agents=[world.eva])
    body = client(world.person).get(f"/api/connections/{conn.pk}/share").json()
    assert body["statement"] == conn.statement and body["statement_sig"] == conn.statement_sig != ""


def test_share_state_names_an_agents_connections_owner_agent(world):
    mine = client(world.person).get(f"/api/connections/{world.persons().pk}/share").json()
    assert mine["owner_agent"] is None
    evas = client(world.person).get(f"/api/connections/{world.evas().pk}/share").json()
    assert evas["owner_agent"] == {"id": str(world.eva.pk), "slug": "eva", "name": "Eva"}
    assert evas["owner_agent"] in evas["agents"]


def _written_by(conn, name, key, ecdh):
    """`conn`'s envelope as box `key` last wrote it, to everyone it is sealed to now."""
    rs = rcpt.recipients(conn)
    pubs = {(k, t): zk.public_key(j) for k, t, j in rs}
    sig = conn_ref.writer_sig(conn.binding, name, conn.sealed_digest, conn.sealed, ecdh, "device", key.ecdh_jkt, pubs)
    Connection.objects.filter(pk=conn.pk).update(sealed_sig=sig)
    conn.refresh_from_db()
    return conn


def test_a_pruned_writer_is_still_vouched_for_so_the_owner_can_re_share_without_reconnecting(world):
    """Review Focus 3: echo's box wrote last, then jj stopped administering echo. It is no
    candidate now and echo is no agent to choose; the browser must still prove that writer."""
    conn = world.shared(world.persons(), agents=[world.eva, world.echo])
    conn = _written_by(conn, "refresh", world.echo_box, world.echo_ecdh)
    revoke_admin(world.person, world.echo)
    assert rcpt.prune(conn) == ["echo-cloud"]
    conn.refresh_from_db()
    assert ("device", world.echo_box.ecdh_jkt) not in zk_seal.sealed_targets(conn.sealed)
    assert conn.sealed_sig["by"]["target"] == world.echo_box.ecdh_jkt

    body = client(world.person).get(f"/api/connections/{conn.pk}/share").json()
    assert world.echo_box.ecdh_jkt not in {c["target"] for c in body["candidates"]}
    assert str(world.echo.pk) not in {a["id"] for a in body["agents"]}
    [e] = [e for e in body["endorsements"] if e["target"] == world.echo_box.ecdh_jkt]
    assert e["agent_id"] == str(world.echo.pk) and e["key_jkt"] == world.echo_box.ecdh_jkt
    assert e["by_public_jwk"] == zk_ref.jwk(world.person_priv.public_key())
    assert zk_ref.verify_endorsement(e, str(world.echo.pk), world.person_priv.public_key())

    r = _put(client(world.person), conn, browser_share(conn, world.person, world.person_priv, agents=[world.eva]))
    assert r.status_code == 200, r.json()
    assert r.json()["statement"]["agents"] == ["eva"]


def test_a_writer_vouched_for_as_a_candidate_is_listed_once(world):
    conn = world.shared(world.persons(), agents=[world.eva])
    conn = _written_by(conn, "refresh", world.eva_box, world.eva_ecdh)
    body = client(world.person).get(f"/api/connections/{conn.pk}/share").json()
    assert len([e for e in body["endorsements"]
                if e["target"] == world.eva_box.ecdh_jkt and e["agent_id"] == str(world.eva.pk)]) == 1


def test_a_malformed_endorsers_list_is_a_bad_statement(world):
    conn = world.persons()
    body = browser_share(conn, world.person, world.person_priv, agents=[world.eva])
    st = {**body["statement"], "endorsers": ["short"]}
    refused(conn, world.person, "bad_statement", **{**body, "statement": st})


def test_share_state_offers_every_machine_and_each_endorsers_key(vouched):
    """The browser decides whom to trust, so the server shows it all: a machine no
    trusted person vouches for yet is still a candidate, with who did vouch."""
    world = vouched
    conn = world.shared(world.persons(), agents=[world.eva], endorsers=[world.ada_jkt])
    r = client(world.person).get(f"/api/connections/{conn.pk}/share")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["endorsers"] == [world.ada_jkt] == body["statement"]["endorsers"]
    assert world.box2.ecdh_jkt in {c["target"] for c in body["candidates"]}
    [e] = [e for e in body["endorsements"] if e["target"] == world.box2.ecdh_jkt]
    assert e["by_public_jwk"] == zk_ref.jwk(world.ada_priv.public_key()) and e["agent_id"] == str(world.eva.pk)


def test_share_state_before_any_share_still_offers_a_machine_only_someone_else_vouches_for(vouched):
    world, conn = vouched, vouched.persons()
    body = client(world.person).get(f"/api/connections/{conn.pk}/share").json()
    assert body["endorsers"] == [] and body["statement"] is None
    assert world.box2.ecdh_jkt in {c["target"] for c in body["candidates"]}


def test_the_grant_service_is_gone():
    assert not hasattr(services, "grant") and not hasattr(services, "revoke_grant")


# ---- a person's connection on the agent's own tab -----------------------------------

def test_an_agents_tab_lists_a_persons_connection_lent_to_it(world):
    conn = world.shared(world.persons(), agents=[world.eva], my_computers=[world.computer.ecdh_jkt])
    # op administers eva and does not own the connection.
    resp = client(world.op).get("/api/agents/eva/connections")
    assert resp.status_code == 200, resp.content
    [row] = resp.json()["connections"]
    assert row["id"] == str(conn.pk)
    assert row["granted_from"] == "jj@dimagi.com"
    assert row["usable"] is True
    # The owner's own computers are their business, not this agent's other admins'.
    assert row["my_machines"] == [] and row["statement"]["my_computers"] == []
    assert world.computer.runner.name not in row["holders"] and "eva-cloud" in row["holders"]


def test_a_lent_row_does_not_show_the_owners_computers_waiting(world):
    world.shared(world.persons(), agents=[world.eva], my_computers=[world.computer.ecdh_jkt],
                 to=[("device", world.eva_box.ecdh_jkt, world.eva_box.ecdh_public_jwk)])
    [mine] = client(world.person).get("/api/connections/").json()["connections"]
    assert {"agent": "", "runner": world.computer.runner.name} in mine["waiting"]  # its owner sees it
    [row] = client(world.op).get("/api/agents/eva/connections").json()["connections"]
    assert [w for w in row["waiting"] if not w["agent"]] == []
    assert world.computer.runner.name not in [w["runner"] for w in row["waiting"]]


def test_settings_says_when_a_connection_is_sealed_to_a_replaced_key(world):
    world.shared(world.persons(), agents=[world.eva])
    [row] = client(world.person).get("/api/connections/").json()["connections"]
    assert row["owner_key_replaced"] is False
    vault_key(world.person)
    [row] = client(world.person).get("/api/connections/").json()["connections"]
    assert row["owner_key_replaced"] is True
    # Not told to the admins of an agent it is lent to.
    [lent] = client(world.op).get("/api/agents/eva/connections").json()["connections"]
    assert lent["owner_key_replaced"] is False


def test_an_agents_own_connection_says_its_key_was_replaced(world):
    world.shared(world.evas(), agents=[world.eva])
    vault_key(world.person)
    [row] = client(world.op).get("/api/agents/eva/connections").json()["connections"]
    assert row["owner_key_replaced"] is True


def test_a_lent_persons_connection_is_unusable_once_the_grant_lapses(world):
    world.shared(world.persons(), agents=[world.eva])
    Agent.objects.filter(pk=world.eva.pk).update(owner=world.op)  # jj no longer administers eva
    [row] = client(world.op).get("/api/agents/eva/connections").json()["connections"]
    assert row["usable"] is False


def test_an_agents_connection_lent_to_another_names_the_owning_agent(world):
    conn = world.shared(world.evas(), agents=[world.eva, world.echo])
    [row] = client(world.op).get("/api/agents/echo/connections").json()["connections"]
    assert row["id"] == str(conn.pk) and row["granted_from"] == "eva" and row["usable"] is True


def test_withdrawing_the_last_grant_through_the_route_empties_the_statement(world):
    conn = world.shared(world.persons(), agents=[world.eva])
    body = browser_share(conn, world.person, world.person_priv, agents=[])
    resp = client(world.person).put(f"/api/connections/{conn.pk}/share", data=json.dumps(body),
                                    content_type="application/json")
    assert resp.status_code == 200, resp.content
    conn.refresh_from_db()
    assert conn.state == "active" and conn.statement["agents"] == [] and conn.statement_version == 2
    assert not conn.grants.exists()
    assert zk_seal.sealed_targets(conn.sealed) == {("member", str(world.person.pk))}
    assert services.providers_for_agent(world.eva) == []


def test_settings_lists_an_agent_a_share_cannot_reach(world):
    AgentVaultEndorsement.objects.filter(target=world.echo_box.ecdh_jkt).delete()
    world.shared(world.persons(), agents=[world.eva, world.echo])
    [row] = client(world.person).get("/api/connections/").json()["connections"]
    assert row["unreachable"] == [{"agent": "echo", "reason": "unvouched"}]
    assert row["statement"]["agents"] == ["echo", "eva"] and row["waiting"] == []
