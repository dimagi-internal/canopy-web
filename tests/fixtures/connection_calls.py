"""Calls a machine makes on the connections routes (a runner token plus a device
proof), and the world they run in. Shared by the tokens and audit tests."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

from canopy_sdk.jose import make_dpop_proof
from django.test import Client
from django.utils import timezone

from apps.agents.credentials import vault_engine, zk, zk_seal
from apps.connections import recipients as rcpt
from apps.connections.models import Connection
from apps.harness.models import Runner, RunnerAccessToken, Turn
from apps.tokens.models import PersonalToken
from tests.fixtures.connections import ShareWorld, sealed_to_owner, sf_app
from tests.fixtures.vault_zk import conn_ref


class TokensWorld(ShareWorld):
    """ShareWorld, plus a second cloud box for eva (`box_b`) and the app's secret
    `app-secret`. `box_a` is eva's first cloud box."""

    def __init__(self):
        super().__init__()
        self.eva_box2, _, _ = self._box("eva-cloud-2", Runner.CLOUD, self.op, self.eva)
        sf_app(self.ws, secret="app-secret")
        self.app.refresh_from_db()
        self.box_a, self.box_b = "eva-cloud", "eva-cloud-2"

    def sealed_to_owner_in(self, ws) -> Connection:
        return sealed_to_owner({"user": self.person}, self.person, app=sf_app(ws))


def _runner_of(world, name):
    return world.machines[name][0].runner


def _turn(agent, runner):
    return Turn.objects.create(agent=agent, origin=Turn.ORIGIN_EMAIL, prompt="x",
                               idempotency_key=uuid.uuid4().hex, status=Turn.CLAIMED, claimed_by=runner)


def _call(method, url, *, bearer, sign=None, body=None):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {bearer}"}
    if sign is not None:
        # The proof names the URL without its query (`htu`).
        headers["HTTP_DPOP"] = make_dpop_proof(sign, method, f"http://testserver{url.split('?')[0]}")
    c = Client()
    if method == "GET":
        return c.get(url, **headers)
    return getattr(c, method.lower())(url, data=json.dumps(body or {}), content_type="application/json",
                                      **headers)


def runner_call(world, name, method, path, body=None, *, proof=True, sign_as=None):
    """A runner route, as box `name`: its own runner token, and a device proof by its
    key (by `sign_as`'s key instead, when given)."""
    key, _, _ = world.machines[name]
    raw, _ = RunnerAccessToken.mint(key)
    sign = world.machines[sign_as or name][1] if proof else None
    return _call(method, f"/api/harness/runners/{key.runner_id}{path}", bearer=raw, sign=sign, body=body)


def material_for(world, name, *, agent=None, turn=None, provider="salesforce", **kw):
    turn = turn or _turn(agent or world.eva, _runner_of(world, name))
    return runner_call(world, name, "GET", f"/turns/{turn.pk}/connections/{provider}", **kw)


def lease(world, name, conn=None, lease_id=None):
    conn = conn or world.conn
    return runner_call(world, name, "POST", f"/connections/{conn.pk}/refresh-lease",
                       {"lease_id": lease_id} if lease_id else {})


def machine_write(world, conn, name, lease_id, *, to=None, rotated=False, expect=None):
    """The write-back body box `name` sends after refreshing: the new access token
    sealed to `to` (default: everyone canopy-web lists), with its writer MACs, and
    the refresh token re-sealed likewise when it `rotated`."""
    key, _, ecdh = world.machines[name]
    conn.refresh_from_db()
    rs = rcpt.recipients(conn) if to is None else to
    pubs = {(k, t): zk.public_key(j) for k, t, j in rs}
    plain = json.dumps({"v": 1, "access_token": "00D!fresh", "expires_at": 1793501800,
                        "instance_url": conn.instance_url, "account_id": conn.account_id, "scope": "api",
                        "owner_key": conn.owner_key, "statement_floor": conn.statement_version})
    access = zk_seal.seal_to(plain, agent_id=conn.binding, name="access", kv=0, recipients=rs)
    body = {"lease_id": lease_id, "access_sealed": access,
            "access_sig": conn_ref.writer_sig(conn.binding, "access", vault_engine.value_digest(conn.access_sealed),
                                              access, ecdh, "device", key.ecdh_jkt, pubs),
            "access_expires_at": (timezone.now() + timedelta(minutes=30)).isoformat(),
            "expect": conn.sealed_digest if expect is None else expect, "rotated": rotated}
    if rotated:
        sealed = zk_seal.seal_to(json.dumps({"refresh_token": "5Aep_rotated"}), agent_id=conn.binding,
                                 name="refresh", kv=0, recipients=rs)
        body["sealed"] = sealed
        body["sealed_sig"] = conn_ref.writer_sig(conn.binding, "refresh", conn.sealed_digest, sealed, ecdh,
                                                 "device", key.ecdh_jkt, pubs)
    return body


def write_back(world, name, body, conn=None):
    conn = conn or world.conn
    return runner_call(world, name, "PUT", f"/connections/{conn.pk}/sealed", body)




def _pat(user):
    raw, _ = PersonalToken.create_for_user(user=user, label="cli")
    return raw


def person_call(world, method, path, body=None, *, machine="jj-computer", user=None, proof=True):
    sign = world.machines[machine][1] if proof else None
    return _call(method, f"/api/me/connections{path}", bearer=_pat(user or world.person), sign=sign, body=body)


def reseal_body(world, conn, name, *, to=None):
    """What box `name` sends (route 14) after opening the refresh envelope: the same
    plaintext re-sealed to `to` (default: everyone canopy-web lists), with writer MACs."""
    key, _, ecdh = world.machines[name]
    conn.refresh_from_db()
    rs = rcpt.recipients(conn) if to is None else to
    pubs = {(k, t): zk.public_key(j) for k, t, j in rs}
    sealed = zk_seal.seal_to(json.dumps({"refresh_token": "same"}), agent_id=conn.binding, name="refresh", kv=0,
                             recipients=rs)
    return {"sealed": sealed,
            "sealed_sig": conn_ref.writer_sig(conn.binding, "refresh", conn.sealed_digest, sealed, ecdh, "device",
                                              key.ecdh_jkt, pubs),
            "access_sealed": "", "access_sig": None, "expect": conn.sealed_digest}
