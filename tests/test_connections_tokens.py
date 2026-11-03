"""Material, the one refresh lease, and compare-and-swap write-back (contract §0.6
routes 7-12).

A machine the owner's statement allows reads the material — the sealed envelopes,
the signed statement and the app's client — takes the single 60 s refresh lease,
refreshes at the provider itself and writes back new envelopes against the
digest it read. canopy-web never sees a token: it checks who is asking (a device
proof from a machine that may hold the connection now, not merely one it was
once sealed to), the lease, the digest, and that the new envelopes reach exactly
the recipients still allowed.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import Client
from django.utils import timezone

from apps.agents.credentials import zk_endorse, zk_seal
from apps.agents.models import AgentVaultEndorsement
from apps.connections import recipients as rcpt
from apps.connections.models import Connection
from apps.events.models import Event
from apps.harness.models import Runner, RunnerAccessToken, RunnerAssignment
from apps.mcp import api_tools
from apps.passkeys.models import UserVaultKeyset
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connection_calls import (
    TokensWorld,
    _runner_of,
    _turn,
    lease,
    machine_write,
    material_for,
    person_call,
    runner_call,
    write_back,
)
from tests.fixtures.connections import FAKE_REFRESH, revoke_admin, sf_app
from tests.fixtures.vault_zk import ref as zk_ref

pytestmark = pytest.mark.django_db


@pytest.fixture
def world():
    return TokensWorld()


@pytest.fixture
def shared(world):
    """eva's own Salesforce, shared to every machine that runs eva."""
    world.conn = world.shared(world.evas(), agents=[world.eva])
    return world


def _events(kind):
    return list(Event.objects.filter(kind=f"connections.{kind}").order_by("pk"))


# ---- material ----------------------------------------------------------------------

def test_material_is_ciphertext_plus_the_app_client(shared):
    world = shared
    r = material_for(world, world.box_a)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["sealed"].startswith("zks1:") and body["app"]["client_secret"] == "app-secret"
    assert FAKE_REFRESH not in json.dumps(body)
    conn = world.conn
    conn.refresh_from_db()
    assert body["binding"] == f"connection:{conn.pk}" and body["sealed_digest"] == conn.sealed_digest
    assert body["app"]["client_id"] == world.app.client_id and body["app"]["options"]["access_token_seconds"] == 1800
    assert body["statement"] == conn.statement and body["statement_sig"] == conn.statement_sig
    assert body["owner"] == {"user_id": str(world.person.pk), "public_jwk": zk_ref.jwk(world.person_priv.public_key())}
    assert {(x["kind"], x["target"], x["runner_name"]) for x in body["recipients"]} >= {
        ("member", str(world.person.pk), ""), ("device", world.eva_box.ecdh_jkt, "eva-cloud")}
    assert body["workspace"] == "w" and body["rotates_refresh_token"] is True
    assert body["lease"] is None and body["access_sealed"] == "" and body["access_expires_at"] is None


def test_material_without_device_proof_is_401(shared):
    world = shared
    r = material_for(world, world.box_a, proof=False)
    assert r.status_code == 401 and r.json()["error"] == "device_proof_required"
    # Another box's key does not go with this box's runner token.
    r = material_for(world, world.box_a, sign_as=world.box_b)
    assert r.status_code == 401 and r.json()["error"] == "device_proof_required"


def test_an_instance_role_proof_is_a_device_proof(shared, monkeypatch):
    """An EC2 box proves its instance role instead of signing a DPoP proof."""
    from apps.harness import aws_iam_proof
    world = shared
    key = world.machines[world.box_a][0]
    monkeypatch.setattr(aws_iam_proof, "verify_caller", lambda raw, request: "arn:role")
    monkeypatch.setattr(aws_iam_proof, "known_key", lambda arn: key)
    turn = _turn(world.eva, key.runner)
    raw, _ = RunnerAccessToken.mint(key)
    r = Client().get(f"/api/harness/runners/{key.runner_id}/turns/{turn.pk}/connections/salesforce",
                     HTTP_AUTHORIZATION=f"Bearer {raw}", HTTP_X_CANOPY_AWS_PROOF="signed-sts-call")
    assert r.status_code == 200, r.content


def test_material_for_a_waiting_machine_is_409_waiting(world):
    world.conn = world.shared(world.evas(), agents=[world.eva],
                              to=[("device", world.eva_box.ecdh_jkt, world.eva_box.ecdh_public_jwk)])
    assert material_for(world, world.box_a).status_code == 200
    r = material_for(world, world.box_b)
    assert r.status_code == 409 and r.json()["error"] == "waiting"


def test_material_refused_for_an_agent_whose_lender_left(world):
    """Review Focus 3: echo's box still holds the envelope, but jj no longer
    administers echo — sealed to is not allowed."""
    world.conn = world.shared(world.persons(), agents=[world.eva, world.echo])
    revoke_admin(world.person, world.echo)
    assert ("device", world.echo_box.ecdh_jkt) in zk_seal.sealed_targets(Connection.objects.get().sealed)
    r = material_for(world, "echo-cloud", agent=world.echo)
    assert r.status_code == 409 and r.json()["error"] == "not_connected"
    assert material_for(world, world.box_a).status_code == 200


def test_material_refused_to_the_last_holder_prune_kept_for_a_revoke(world):
    world.conn = world.shared(world.persons(), agents=[world.echo])
    revoke_admin(world.person, world.echo)
    rcpt.prune(world.conn)
    assert ("device", world.echo_box.ecdh_jkt) in zk_seal.sealed_targets(Connection.objects.get().sealed)
    r = material_for(world, "echo-cloud", agent=world.echo)
    assert r.status_code == 409 and r.json()["error"] == "not_connected"


def test_unshared_connection_is_handed_to_no_machine(world):
    """Review Focus 5: the owner closed the tab before sharing."""
    world.conn = world.evas()
    r = material_for(world, world.box_a)
    assert r.status_code == 409 and r.json()["error"] == "not_connected"
    Connection.objects.filter(pk=world.conn.pk).update(state=Connection.REVOKING)
    r = material_for(world, world.box_a)
    assert r.status_code == 409 and r.json()["error"] == "not_connected"


def test_material_read_records_token_issued_once_per_turn_with_runner(shared):
    world = shared
    runner = _runner_of(world, world.box_a)
    turn = _turn(world.eva, runner)
    assert material_for(world, world.box_a, turn=turn).status_code == 200
    assert material_for(world, world.box_a, turn=turn).status_code == 200
    [ev] = _events("token_issued")
    assert ev.payload["to"] == "turn" and ev.payload["turn"] == str(turn.pk)
    assert ev.payload["runner"] == str(runner.pk) and ev.payload["machine"] == "eva-cloud"
    assert material_for(world, world.box_a).status_code == 200      # another turn: its own record
    assert len(_events("token_issued")) == 2
    world.conn.refresh_from_db()
    assert world.conn.last_used_at is not None


def test_a_rotated_app_secret_reaches_the_next_material_read(shared):
    world = shared
    assert material_for(world, world.box_a).json()["app"]["client_secret"] == "app-secret"
    sf_app(world.ws, secret="rotated-secret")
    assert material_for(world, world.box_a).json()["app"]["client_secret"] == "rotated-secret"


def test_a_persons_connection_lent_to_an_agent_is_the_agents(world):
    world.conn = world.shared(world.persons(), agents=[world.eva])
    r = runner_call(world, world.box_a, "GET", f"/turns/{_turn(world.eva, _runner_of(world, world.box_a)).pk}"
                                                "/connections")
    assert r.json()["providers"] == ["salesforce"]
    assert material_for(world, world.box_a).status_code == 200


# ---- the lease ---------------------------------------------------------------------

def test_one_lease_holder_at_a_time(shared):
    world = shared
    a = lease(world, world.box_a)
    assert a.status_code == 200 and lease(world, world.box_b).json()["error"] == "lease_held"
    held = lease(world, world.box_b)
    assert held.status_code == 409 and 0 < held.json()["retry_after"] <= 60
    # A second process on the same box, without the id, waits too.
    assert lease(world, world.box_a).json()["error"] == "lease_held"
    lid = a.json()["lease_id"]
    assert lease(world, world.box_a, lease_id=lid).json()["lease_id"] == lid      # renewed
    assert material_for(world, world.box_a).json()["lease"]["mine"] is True
    assert material_for(world, world.box_b).json()["lease"]["mine"] is False
    r = runner_call(world, world.box_a, "DELETE", f"/connections/{world.conn.pk}/refresh-lease/{lid}")
    assert r.status_code == 204
    assert lease(world, world.box_b).status_code == 200


def test_lease_answers_ttl_seconds_not_only_a_timestamp(shared):
    """Review Focus 1: a box with a skewed clock renews on `ttl_seconds`."""
    body = lease(shared, shared.box_a).json()
    assert body["ttl_seconds"] == 60 and body["expires_at"]


def test_a_refused_refresh_ends_the_lease_with_no_false_alarm_for_the_next_taker(shared, caplog):
    """The holder was refused and is fine: it did not die between refreshing and writing back."""
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    world = shared
    lease_id = lease(world, world.box_a).json()["lease_id"]
    r = runner_call(world, world.box_a, "POST", f"/connections/{world.conn.pk}/reconnect-required",
                    {"lease_id": lease_id, "error": "invalid_grant", "detail": "ip restricted"})
    assert r.status_code == 204, r.content
    world.conn.refresh_from_db()
    assert world.conn.needs_reconnect and world.conn.lease_id is None and world.conn.lease_expires_at is None
    assert "ip restricted" in caplog.text or _events("reconnect_required")[0].payload["detail"] == "ip restricted"
    # Another machine, at once: it is not told to wait, and nobody is accused of dying.
    assert lease(world, world.box_b).status_code == 200
    assert _events("lease_expired_unwritten") == []
    assert "lease_expired_unwritten" not in caplog.text


def test_a_provider_error_is_noted_with_its_description_and_keeps_the_lease(shared, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    world = shared
    lease_id = lease(world, world.box_a).json()["lease_id"]
    r = runner_call(world, world.box_a, "POST", f"/connections/{world.conn.pk}/reconnect-required",
                    {"lease_id": lease_id, "error": "invalid_client", "detail": "client disabled"})
    assert r.status_code == 204, r.content
    world.conn.refresh_from_db()
    assert not world.conn.needs_reconnect and str(world.conn.lease_id) == lease_id
    assert "connections.provider_error" in caplog.text and "client disabled" in caplog.text


def test_taking_an_expired_unwritten_lease_records_lease_expired_unwritten(shared):
    world = shared
    lease(world, world.box_a)
    Connection.objects.filter(pk=world.conn.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    assert lease(world, world.box_b).status_code == 200
    [ev] = _events("lease_expired_unwritten")
    assert ev.payload["machine"] == "eva-cloud"


def test_a_lease_on_a_box_that_may_not_hold_the_connection_is_refused(shared):
    r = lease(shared, "echo-cloud")
    assert r.status_code == 403 and r.json()["error"] == "not_a_recipient"


# ---- write-back --------------------------------------------------------------------

def test_write_back_stores_the_new_access_envelope(shared):
    world = shared
    lid = lease(world, world.box_a).json()["lease_id"]
    body = machine_write(world, world.conn, world.box_a, lid)
    r = write_back(world, world.box_a, body)
    assert r.status_code == 200, r.content
    world.conn.refresh_from_db()
    assert r.json()["sealed_digest"] == world.conn.sealed_digest
    assert world.conn.access_sealed == body["access_sealed"] and world.conn.access_sig == body["access_sig"]
    assert world.conn.access_expires_at is not None and world.conn.lease_written is True
    [ev] = _events("refreshed")
    assert ev.payload["rotated"] is False and ev.payload["machine"] == "eva-cloud"


def test_write_back_with_a_lost_lease_is_refused(shared):
    world = shared
    lid = lease(world, world.box_a).json()["lease_id"]
    Connection.objects.filter(pk=world.conn.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    # R13: expired, but nobody took it since — still accepted.
    assert write_back(world, world.box_a, machine_write(world, world.conn, world.box_a, lid)).status_code == 200
    lid = lease(world, world.box_a).json()["lease_id"]
    Connection.objects.filter(pk=world.conn.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    assert lease(world, world.box_b).status_code == 200
    before = Connection.objects.get().access_sealed
    r = write_back(world, world.box_a, machine_write(world, world.conn, world.box_a, lid))
    assert r.status_code == 409 and r.json()["error"] == "lease_lost"
    assert Connection.objects.get().access_sealed == before


def test_write_back_cas_on_sealed_digest(shared):
    world = shared
    lid = lease(world, world.box_a).json()["lease_id"]
    r = write_back(world, world.box_a, machine_write(world, world.conn, world.box_a, lid, expect="0" * 64))
    assert r.status_code == 409 and r.json()["error"] == "zk_value_changed"


def test_write_back_must_keep_every_still_eligible_recipient(shared):
    world = shared
    lid = lease(world, world.box_a).json()["lease_id"]
    to = [x for x in rcpt.recipients(world.conn) if x[1] != world.eva_box2.ecdh_jkt]
    r = write_back(world, world.box_a, machine_write(world, world.conn, world.box_a, lid, to=to))
    assert r.status_code == 422 and r.json()["error"] == "zk_rewrap_required"


def test_write_back_to_a_stranger_is_refused(shared):
    world = shared
    lid = lease(world, world.box_a).json()["lease_id"]
    stranger = ec.generate_private_key(ec.SECP256R1()).public_key()
    to = [*rcpt.recipients(world.conn), ("device", zk_ref.jkt(stranger), zk_ref.jwk(stranger))]
    r = write_back(world, world.box_a, machine_write(world, world.conn, world.box_a, lid, to=to))
    assert r.status_code == 422 and r.json()["error"] == "foreign_reader"


def test_write_back_by_another_writer_is_refused(shared):
    world = shared
    lid = lease(world, world.box_a).json()["lease_id"]
    body = machine_write(world, world.conn, world.box_b, lid)      # MACs by box_b's key
    r = write_back(world, world.box_a, body)
    assert r.status_code == 422 and r.json()["error"] == "bad_writer_sig"


def test_write_back_to_a_revoking_connection_is_refused(shared):
    """Review Focus 2: disconnected mid-refresh — nothing stored."""
    world = shared
    lid = lease(world, world.box_a).json()["lease_id"]
    body = machine_write(world, world.conn, world.box_a, lid, rotated=True)
    Connection.objects.filter(pk=world.conn.pk).update(state=Connection.REVOKING)
    before = Connection.objects.get()
    r = write_back(world, world.box_a, body)
    assert r.status_code == 409 and r.json()["error"] == "revoking"
    after = Connection.objects.get()
    assert (after.sealed, after.access_sealed) == (before.sealed, before.access_sealed) and after.access_sealed == ""


def test_rotated_write_back_records_refreshed_rotated_true(shared):
    world = shared
    lid = lease(world, world.box_a).json()["lease_id"]
    body = machine_write(world, world.conn, world.box_a, lid, rotated=True)
    r = write_back(world, world.box_a, body)
    assert r.status_code == 200, r.content
    world.conn.refresh_from_db()
    assert world.conn.sealed == body["sealed"] and world.conn.sealed_sig == body["sealed_sig"]
    assert r.json()["sealed_digest"] == hashlib.sha256(body["sealed"].encode()).hexdigest() == world.conn.sealed_digest
    [ev] = _events("refreshed")
    assert ev.payload["rotated"] is True and ev.payload["machine"] == "eva-cloud"


def test_invalid_grant_marks_needs_reconnect_other_errors_do_not(shared):
    world = shared
    lid = lease(world, world.box_a).json()["lease_id"]
    url = f"/connections/{world.conn.pk}/reconnect-required"
    r = runner_call(world, world.box_a, "POST", url, {"lease_id": lid, "error": "temporarily_unavailable"})
    assert r.status_code == 204
    world.conn.refresh_from_db()
    assert world.conn.refresh_failed_at is None and not _events("reconnect_required")
    r = runner_call(world, world.box_b, "POST", url, {"lease_id": lid, "error": "invalid_grant"})
    assert r.status_code == 409 and r.json()["error"] == "lease_lost"
    assert runner_call(world, world.box_a, "POST", url, {"lease_id": lid, "error": "invalid_grant"}).status_code == 204
    world.conn.refresh_from_db()
    assert world.conn.needs_reconnect and len(_events("reconnect_required")) == 1
    r = material_for(world, world.box_a)
    assert r.status_code == 409 and r.json()["error"] == "needs_reconnect"


# ---- a person's own computer -------------------------------------------------------

def test_person_material_needs_a_plain_pat_and_their_own_machine(world):
    world.conn = world.shared(world.persons(), my_computers=[world.computer.ecdh_jkt])
    c = Client()
    c.force_login(world.person)
    r = c.get("/api/me/connections/salesforce/material")
    assert r.status_code == 403 and r.json()["error"] == "token_route_needs_pat"
    r = person_call(world, "GET", "/salesforce/material", proof=False)
    assert r.status_code == 401 and r.json()["error"] == "device_proof_required"
    r = person_call(world, "GET", "/salesforce/material", machine="ada-computer")
    assert r.status_code == 403 and r.json()["error"] == "machine_not_enrolled"
    assert "canopy vault device --enroll" in r.json()["detail"]
    r = person_call(world, "GET", "/salesforce/material")
    assert r.status_code == 200, r.content
    assert r.json()["app"]["client_secret"] == "app-secret" and FAKE_REFRESH not in r.content.decode()
    # A person's connection belongs to no workspace: its record is a log line, not an Event.
    assert not Event.objects.filter(kind="connections.token_issued").exists()


def test_person_material_refuses_a_machine_not_on_my_computers(world):
    world.conn = world.shared(world.persons(), agents=[world.eva])
    r = person_call(world, "GET", "/salesforce/material")
    assert r.status_code == 403
    assert r.json()["error"] == "not_on_my_computers" and "Use on my computers" in r.json()["detail"]


def test_ambiguous_person_material_names_the_workspaces(world):
    world.conn = world.shared(world.persons(), my_computers=[world.computer.ecdh_jkt])
    w2 = Workspace.objects.create(slug="w2", display_name="W2", created_by=world.person)
    WorkspaceMembership.objects.create(workspace=w2, user=world.person, role=WorkspaceMembership.EDITOR)
    world.sealed_to_owner_in(w2)
    r = person_call(world, "GET", "/salesforce/material")
    assert r.status_code == 409 and r.json()["error"] == "ambiguous" and r.json()["workspaces"] == ["w", "w2"]
    assert person_call(world, "GET", "/salesforce/material?workspace=w").status_code == 200


def test_person_refreshes_and_writes_back_on_their_own_computer(world):
    world.conn = world.shared(world.persons(), my_computers=[world.computer.ecdh_jkt])
    cid = world.conn.pk
    lid = person_call(world, "POST", f"/{cid}/refresh-lease").json()["lease_id"]
    body = machine_write(world, world.conn, "jj-computer", lid)
    r = person_call(world, "PUT", f"/{cid}/sealed", body)
    assert r.status_code == 200, r.content
    assert person_call(world, "POST", f"/{cid}/reconnect-required",
                       {"lease_id": lid, "error": "invalid_grant"}).status_code == 204
    assert person_call(world, "DELETE", f"/{cid}/refresh-lease/{lid}").status_code == 204
    world.conn.refresh_from_db()
    assert world.conn.needs_reconnect and world.conn.lease_id is None
    # Someone else's connection id is a 404 to me.
    assert person_call(world, "POST", f"/{cid}/refresh-lease", user=world.ada,
                       machine="ada-computer").status_code == 404


# ---- readers can always check the last writer ----------------------------------------

def _last_written_by_echos_box(world) -> Connection:
    """Shared to eva and echo; echo's box then refreshed and rotated it, so it is the
    writer of both envelopes; then jj stopped administering echo and the box was pruned."""
    world.conn = world.shared(world.persons(), agents=[world.eva, world.echo])
    lease_id = lease(world, "echo-cloud").json()["lease_id"]
    r = write_back(world, "echo-cloud", machine_write(world, world.conn, "echo-cloud", lease_id, rotated=True))
    assert r.status_code == 200, r.content
    revoke_admin(world.person, world.echo)
    assert "echo-cloud" in rcpt.prune(world.conn)
    world.conn.refresh_from_db()
    assert world.conn.sealed_sig["by"]["target"] == world.conn.access_sig["by"]["target"] == world.echo_box.ecdh_jkt
    return world.conn


def _writer_endorsements(body, world) -> list[dict]:
    return [e for e in body["endorsements"] if e["target"] == world.echo_box.ecdh_jkt]


def test_material_includes_a_pruned_writers_endorsement(world):
    conn = _last_written_by_echos_box(world)
    r = material_for(world, world.box_a)
    assert r.status_code == 200, r.content
    body = r.json()
    assert world.echo_box.ecdh_jkt not in {x["target"] for x in body["recipients"]}
    [e] = _writer_endorsements(body, world)
    assert (e["kind"], e["key_jkt"], e["agent_id"]) == ("device", world.echo_box.ecdh_jkt, str(world.echo.pk))
    # A reader checks it with what it was handed: the writer's key travels in `by`.
    assert e["by_public_jwk"] == zk_ref.jwk(world.person_priv.public_key())
    assert zk_endorse.verify(world.echo.pk, kind="device", target=e["target"], key_jkt=e["key_jkt"], sig=e["sig"],
                             endorser_jwk=e["by_public_jwk"])
    assert body["sealed_sig"]["by"]["public_jwk"] == world.echo_box.ecdh_public_jwk
    assert conn.statement["agents"] == sorted([str(world.eva.pk), str(world.echo.pk)])


def test_a_writer_that_stopped_running_the_agent_still_has_its_endorsement_listed(world):
    _last_written_by_echos_box(world)
    RunnerAssignment.objects.filter(runner=world.echo_box.runner).delete()
    assert len(_writer_endorsements(material_for(world, world.box_a).json(), world)) == 1


def test_only_the_writers_own_endorsements_are_added(world):
    # echo-cloud's is there as the writer's; another box of echo's that wrote nothing is not.
    other, _, _ = world._box("echo-cloud-2", Runner.CLOUD, world.op, world.echo)
    _last_written_by_echos_box(world)
    targets = {e["target"] for e in material_for(world, world.box_a).json()["endorsements"]}
    assert world.echo_box.ecdh_jkt in targets and other.ecdh_jkt not in targets


def test_a_writer_whose_endorsement_is_gone_still_gets_an_answer(world):
    _last_written_by_echos_box(world)
    AgentVaultEndorsement.objects.filter(target=world.echo_box.ecdh_jkt).delete()
    r = material_for(world, world.box_a)
    assert r.status_code == 200 and _writer_endorsements(r.json(), world) == []


def test_a_writers_endorsement_whose_signer_has_no_key_is_left_out(world):
    _last_written_by_echos_box(world)
    AgentVaultEndorsement.objects.filter(target=world.echo_box.ecdh_jkt).update(endorser=world.ada)
    UserVaultKeyset.objects.filter(user=world.ada).delete()
    r = material_for(world, world.box_a)
    assert r.status_code == 200 and _writer_endorsements(r.json(), world) == []


def test_a_member_writer_adds_no_endorsement(world):
    world.conn = world.shared(world.persons(), agents=[world.eva])
    world.conn.refresh_from_db()
    assert world.conn.sealed_sig["by"]["kind"] == "member"
    assert {e["target"] for e in material_for(world, world.box_a).json()["endorsements"]} == {
        world.eva_box.ecdh_jkt, world.eva_box2.ecdh_jkt}


# ---- a machine no trusted person vouches for ------------------------------------------

def test_material_refuses_a_machine_the_statement_does_not_vouch_for(world):
    world.conn = world.shared(world.persons(), agents=[world.eva], to=[
        ("device", world.eva_box.ecdh_jkt, world.eva_box.ecdh_public_jwk)])
    AgentVaultEndorsement.objects.filter(target=world.eva_box.ecdh_jkt).delete()
    r = material_for(world, world.box_a)
    assert r.status_code == 409 and r.json()["error"] == "not_connected"


# ---- never a tool ------------------------------------------------------------------

@pytest.mark.parametrize("op", [
    "my_connection_material", "take_my_refresh_lease", "release_my_refresh_lease", "write_back_my_connection",
    "my_connection_reconnect_required", "turn_connection_material", "take_refresh_lease",
    "release_refresh_lease", "write_back_connection", "connection_reconnect_required",
])
def test_token_adjacent_routes_are_not_mcp_tools(op):
    assert op in api_tools.EXCLUDED
