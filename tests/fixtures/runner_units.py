"""A cloud box invited and paired as one isolation unit per workspace tree or agent
(`_invite`, `_approve`, `_pair_units`). Plain helpers, shared by the pairing and
isolation suites.
"""
from __future__ import annotations

from django.test import Client

from tests.fixtures.aws_proof import ROLE
from tests.fixtures.runner_invitations import INVITES, PAIR, PAIR_HTU, _pat_client, _proof

BOX = "cloud-ec2-9"


UNITS = [
    {"name": BOX, "workspace": "connect", "kind": "tree"},
    {"name": f"{BOX}-marsh", "workspace": "marsh", "kind": "tree"},
    {"name": f"{BOX}-agent-eva", "workspace": "connect", "kind": "agent", "slug": "eva"},
]


def _invite(org, units=UNITS, name=BOX, **extra):
    return _pat_client(org["pat"]).post(
        INVITES, data={"name": name, "workspace": "connect", "role_arn": ROLE, "units": units, **extra},
        content_type="application/json")


def _approve(org, inv_id):
    assert org["browser"].post(f"{INVITES}{inv_id}/approve").status_code == 200


def _pair_units(units: dict[str, dict], *, name=BOX):
    """What canopy-pair sends: the box's name, ONE role proof, every unit's keys."""
    body = {"name": name, "kind": "cloud", "proof": _proof(PAIR_HTU), "units": [
        {"name": n, "public_jwk": k["public_jwk"], "ecdh_public_jwk": k.get("ecdh_public_jwk"),
         "ecdh_store": "kms-broker", "host": "ip-10-0-0-9", "capabilities": {"sessions": True}}
        for n, k in units.items()]}
    return Client().post(PAIR, data=body, content_type="application/json")
