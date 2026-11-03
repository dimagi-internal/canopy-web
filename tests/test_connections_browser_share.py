"""What the owner's browser writes, through what canopy-web and a machine check.

`docs/vault/zk-v1/connections_v1_browser_share.json` is written by the browser's own
code (`frontend/src/vault/connectionShare.test.ts`, which also holds it to what that code
writes today). Here it goes through the real share route and the connections REFERENCE,
so the browser's bytes meet the other side's rules. `first` is the share after connect;
`second` a re-share over a machine's write-back, which also re-seals `access`.

The world's ids and keys are the fixture's, because the signed statement names them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.credentials import zk_seal
from apps.agents.models import Agent, AgentAdmin, AgentVaultEndorsement
from apps.connections.models import Connection
from apps.harness.models import Runner, RunnerAssignment
from apps.passkeys.models import UserVaultKeyset
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import sf_app
from tests.fixtures.machines import a_machine
from tests.fixtures.vault_zk import conn_ref
from tests.fixtures.vault_zk import ref as zk_ref

pytestmark = pytest.mark.django_db

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "docs" / "vault" / "zk-v1"
                      / "connections_v1_browser_share.json").read_text())
KEYS = FIXTURE["keys"]


def _priv(name):
    return conn_ref._priv(KEYS[name]["scalar_hex"])


def _box(ws, name, kind, owner, key_name, agent=None):
    runner = Runner.objects.create(name=name, kind=kind, owner=owner, workspace=ws,
                                   last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    if agent is not None:
        RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    key, _, _ = a_machine(runner, owner)
    key.ecdh_public_jwk, key.ecdh_jkt = KEYS[key_name]["public_jwk"], KEYS[key_name]["jkt"]
    key.save()
    return key


def _world() -> tuple[Connection, object]:
    """The connection as `first.state` shows it, and the owner."""
    users = get_user_model().objects
    person = users.create_user(id=int(FIXTURE["users"]["owner"]), username="jj", email="jj@dimagi.com")
    ada = users.create_user(id=int(FIXTURE["users"]["ada"]), username="ada", email="ada@dimagi.com")
    stranger = users.create_user(id=int(FIXTURE["users"]["stranger"]), username="st", email="st@dimagi.com")
    op = users.create_user(username="op", email="op@dimagi.com")
    for user, key in ((person, "owner"), (ada, "ada"), (stranger, "stranger")):
        UserVaultKeyset.objects.create(user=user, public_jwk=KEYS[key]["public_jwk"], seals=[])
    ws = Workspace.objects.create(slug="w", display_name="W", created_by=person)
    for u in (person, ada, stranger, op):
        WorkspaceMembership.objects.create(workspace=ws, user=u, role=WorkspaceMembership.EDITOR)
    eva = Agent.objects.create(id=int(FIXTURE["agents"]["eva"]), slug="eva", name="Eva", workspace=ws, owner=person)
    for u in (op, ada, stranger):
        AgentAdmin.objects.create(agent=eva, user=u)
    _box(ws, "eva-cloud", Runner.CLOUD, op, "eva_box", eva)
    _box(ws, "ada-cloud", Runner.CLOUD, op, "ada_box", eva)
    _box(ws, "stranger-cloud", Runner.CLOUD, op, "stranger_box", eva)
    _box(ws, "jj-computer", Runner.COMPUTER, person, "computer")
    state = FIXTURE["first"]["state"]
    for e in state["endorsements"]:
        AgentVaultEndorsement.objects.create(agent=eva, kind="device", target=e["target"], key_jkt=e["key_jkt"],
                                             endorser_id=int(e["by"]), endorser_jkt=e["by_jkt"], sig=e["sig"])
    conn = Connection.objects.create(
        id=FIXTURE["connection"], app=sf_app(ws), provider="salesforce", account_id=state["account_id"],
        account_label=state["account_label"], instance_url="https://acme.my.salesforce.com", user=person,
        sealed=state["sealed"], sealed_digest=state["sealed_digest"], owner_key=KEYS["owner"]["jkt"], signer=person,
        owner_public_jwk=UserVaultKeyset.objects.get(user=person).public_jwk, owner_user_id=str(person.pk),
        connected_by=person, state=Connection.UNSHARED)
    return conn, person


def _client(user):
    c = Client()
    c.force_login(user)
    grant_step_up(c)
    return c


def _put(user, body):
    return _client(user).put(f"/api/connections/{FIXTURE['connection']}/share", body, content_type="application/json")


def _recipients(*names):
    out = [("member", FIXTURE["users"]["owner"], _priv("owner"))]
    return out + [("device", KEYS[n]["jkt"], _priv(n)) for n in names]


def _check_envelope(stored, sig, name, expect, recipients, want_plain):
    """Each recipient, by the reference: its writer MAC against the owner's key, and the
    plaintext the browser sealed. Exactly these recipients, once each."""
    binding = f"connection:{FIXTURE['connection']}"
    assert sig["by"] == {"kind": "member", "target": FIXTURE["users"]["owner"],
                         "public_jwk": KEYS["owner"]["public_jwk"]}
    assert sig["expect"] == expect
    writer = _priv("owner").public_key()
    entries = conn_ref.sealed_entries(stored)
    assert {(e["kind"], e["target"]) for e in entries} == {(k, t) for k, t, _ in recipients}
    assert zk_seal.sealed_targets(stored) == {(k, t) for k, t, _ in recipients}
    macs = {(m["kind"], m["target"]): m["mac"] for m in sig["macs"]}
    assert set(macs) == {(k, t) for k, t, _ in recipients}
    for kind, target, priv in recipients:
        entry = next(e for e in entries if (e["kind"], e["target"]) == (kind, target))
        assert conn_ref.verify_envelope_mac(binding, name, expect, entry, macs[(kind, target)], priv, writer)
        text = zk_ref.open_sealed_value(stored, binding, name, kind, target, priv)
        assert text == json.dumps(want_plain, separators=(",", ":")), "compact, in the contract's order"


def _refresh_plain(floor):
    return {"v": 1, "provider": "salesforce", "refresh_token": FIXTURE["refresh_token"],
            "client_id": "3MVG9-test-client-id", "login_host": "login.salesforce.com",
            "instance_url": "https://acme.my.salesforce.com", "account_id": FIXTURE["account_id"],
            "owner_key": KEYS["owner"]["jkt"], "issued_at": 1793500000, "scopes": "api refresh_token id",
            "statement_floor": floor}


def _access_plain(floor):
    return {"v": 1, "access_token": FIXTURE["access_token"], "expires_at": 1793501800,
            "instance_url": "https://acme.my.salesforce.com", "account_id": FIXTURE["account_id"], "scope": "api",
            "owner_key": KEYS["owner"]["jkt"], "statement_floor": floor}


def _shared_first():
    conn, person = _world()
    r = _put(person, FIXTURE["first"]["share"])
    assert r.status_code == 200, r.json()
    conn.refresh_from_db()
    return conn, person


def _what_a_candidate_is(c):
    return (c["kind"], c["target"], c["agent_id"], c["runner_name"], c["mine"])


def test_the_browser_was_given_what_the_share_route_answers():
    conn, person = _world()
    r = _client(person).get(f"/api/connections/{conn.pk}/share")
    assert r.status_code == 200, r.json()
    body, state = r.json(), FIXTURE["first"]["state"]
    assert set(body) >= set(state)
    for field in ("id", "binding", "provider", "account_id", "state", "sealed", "sealed_digest", "owner", "agents"):
        assert body[field] == state[field], field
    assert body["statement_sig"] == state["statement_sig"] == "" and body["owner_agent"] is state["owner_agent"] is None
    assert (sorted(map(_what_a_candidate_is, body["candidates"]))
            == sorted(map(_what_a_candidate_is, state["candidates"])))
    assert ({e["sig"]: e["agent_id"] for e in body["endorsements"]}
            == {e["sig"]: e["agent_id"] for e in state["endorsements"]})
    assert {e["by_jkt"] for e in body["endorsements"]} == {e["by_jkt"] for e in state["endorsements"]}


def test_the_first_share_the_browser_writes_is_accepted_and_opens_on_each_machine():
    conn, _ = _shared_first()
    share = FIXTURE["first"]["share"]
    assert conn.state == Connection.ACTIVE and conn.statement == share["statement"]
    assert conn.statement_version == 1
    assert conn_ref.statement_msg(share["statement"]).decode() == FIXTURE["first"]["msg"]
    assert conn_ref.verify_statement(share["statement"], share["sig"], _priv("owner").public_key())
    assert share["statement"]["endorsers"] == [KEYS["ada"]["jkt"]]
    assert share["statement"]["my_computers"] == [KEYS["computer"]["jkt"]]
    assert share["by_jkt"] == KEYS["owner"]["jkt"]
    _check_envelope(conn.sealed, conn.sealed_sig, "refresh", FIXTURE["first"]["state"]["sealed_digest"],
                    _recipients("eva_box", "ada_box", "computer"), _refresh_plain(1))
    # Nothing to re-seal: `access` stays empty until a machine's first refresh.
    assert "access_sealed" not in share and conn.access_sealed == ""


def test_a_reshare_over_a_machines_write_is_accepted_with_access_re_sealed_and_a_computer_withdrawn():
    conn, person = _shared_first()
    state, share = FIXTURE["second"]["state"], FIXTURE["second"]["share"]
    # The machine's write-back, as stored: the browser read this and proved its writer.
    assert state["sealed_sig"]["by"]["target"] == KEYS["eva_box"]["jkt"]
    Connection.objects.filter(pk=conn.pk).update(
        sealed=state["sealed"], sealed_sig=state["sealed_sig"], sealed_digest=state["sealed_digest"],
        access_sealed=state["access_sealed"], access_sig=state["access_sig"], statement=state["statement"],
        statement_sig=state["statement_sig"])
    r = _put(person, share)
    assert r.status_code == 200, r.json()
    conn.refresh_from_db()
    assert conn.statement == share["statement"] and conn.statement_version == 2
    # What the browser verified its device writer's statement by is what the route hands out.
    read = _client(person).get(f"/api/connections/{conn.pk}/share").json()
    assert read["statement_sig"] == share["sig"]
    assert conn.statement["my_computers"] == []
    assert conn_ref.statement_msg(share["statement"]).decode() == FIXTURE["second"]["msg"]
    assert conn_ref.verify_statement(share["statement"], share["sig"], _priv("owner").public_key())
    recipients = _recipients("eva_box", "ada_box")
    _check_envelope(conn.sealed, conn.sealed_sig, "refresh", state["sealed_digest"], recipients, _refresh_plain(2))
    _check_envelope(conn.access_sealed, conn.access_sig, "access", conn_ref.sealed_digest(state["access_sealed"]),
                    recipients, _access_plain(2))


def test_the_route_refuses_a_browser_share_replayed_over_a_newer_write():
    conn, person = _shared_first()
    r = _put(person, FIXTURE["first"]["share"])
    assert r.status_code == 409, r.json()
    assert r.json()["error"] == "statement_stale"
    conn.refresh_from_db()
    assert conn.statement_version == 1
