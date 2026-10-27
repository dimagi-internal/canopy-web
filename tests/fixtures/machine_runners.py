"""A cloud machine invited and paired as one runner per workspace tree or agent
(`_invite`, `_approve`, `_pair_runners`). Plain helpers, shared by the pairing and
isolation suites.
"""
from __future__ import annotations

from django.test import Client

from tests.fixtures.aws_proof import ROLE
from tests.fixtures.runner_invitations import INVITES, PAIR, PAIR_HTU, _pat_client, _proof

BOX = "cloud-ec2-9"


RUNNERS = [
    {"name": BOX, "workspace": "connect", "kind": "tree"},
    {"name": f"{BOX}-marsh", "workspace": "marsh", "kind": "tree"},
    {"name": f"{BOX}-agent-eva", "workspace": "connect", "kind": "agent", "slug": "eva"},
]


def _invite(org, runners=RUNNERS, name=BOX, *, key="runners", **extra):
    """`key`: what the list is sent as — "units" is how an up.sh from before the rename sends it."""
    return _pat_client(org["pat"]).post(
        INVITES, data={"name": name, "workspace": "connect", "role_arn": ROLE, key: runners, **extra},
        content_type="application/json")


def _approve(org, inv_id):
    assert org["browser"].post(f"{INVITES}{inv_id}/approve").status_code == 200


def _pair_runners(runners: dict[str, dict], *, name=BOX, key="runners"):
    """What canopy-pair sends: the box's name, ONE role proof, every runner's keys.
    `key`: what the list is sent as — "units" is how a runner from before the rename sends it."""
    body = {"name": name, "kind": "cloud", "proof": _proof(PAIR_HTU), key: [
        {"name": n, "public_jwk": k["public_jwk"], "ecdh_public_jwk": k.get("ecdh_public_jwk"),
         "ecdh_store": "kms-broker", "host": "ip-10-0-0-9", "capabilities": {"sessions": True}}
        for n, k in runners.items()]}
    return Client().post(PAIR, data=body, content_type="application/json")
