"""Helpers shared by the capability suites: the manifest and REST shorthands."""
from __future__ import annotations

import json

from django.test import Client

from apps.events.models import Event
from apps.tokens.models import PersonalToken

MANIFEST = {
    "version": 1, "name": "android-emulator", "kind": "ec2-ephemeral",
    "description": "Android 14 emulator driven by the ace-mobile CLI.",
    "aws": {"region": "us-east-1", "stack": "canopy-cap-ace-android-emulator"},
    "limits": {"max_concurrent": 4, "max_lease_s": 14400, "idle_release_s": 900, "ready_timeout_s": 900},
    "params": {"state": {"type": "string", "default": "cc-2.63.0", "enum": ["cc-2.62.0", "cc-2.63.0"]}},
    "exec": {"allow": ["ace-mobile"]},
}


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _pat(user) -> Client:
    raw, _ = PersonalToken.create_for_user(user=user, label="deploy")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _json(c, method, path, body=None):
    fn = getattr(c, method)
    if body is None:
        return fn(path)
    return fn(path, data=json.dumps(body), content_type="application/json")


def _publish(c, *manifests):
    return _json(c, "put", "/api/agents/ace/capabilities", {"capabilities": list(manifests or [MANIFEST])})


def _grant(c, **limits):
    return _json(c, "put", "/api/capabilities/grants",
                 {"agent": "ace", "capability": "android-emulator", **limits})


def _acquire(c, **extra):
    return _json(c, "post", "/api/capabilities/leases", {"agent": "ace", "capability": "android-emulator", **extra})


def _kinds(lease_id) -> list[str]:
    return [e.kind for e in Event.objects.filter(source="capabilities").order_by("id")
            if (e.payload or {}).get("lease_id") == str(lease_id)]
