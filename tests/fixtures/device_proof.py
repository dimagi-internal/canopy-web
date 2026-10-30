"""Tests about WHO may resolve an agent's credentials are not about the device proof.

A stored value turns an agent zero-knowledge (`services.start_zero_knowledge`), and every
resolve of a zero-knowledge agent carries a device proof from an approved key whose person
is a member of the agent's vault (tests/test_canopy_vault_device_binding.py and
tests/test_canopy_vault_zk.py pin both). `accept_device_proof` stands in for that proof, so
a test can say what it is actually about.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.agents.credentials import credential_resolve
from apps.harness.models import Runner, RunnerDeviceKey


def _key_on(runner) -> RunnerDeviceKey:
    key, _ = RunnerDeviceKey.objects.get_or_create(
        runner=runner, jkt=f"jkt-proof-{runner.pk}",
        defaults={"kind": RunnerDeviceKey.SOFTWARE, "status": RunnerDeviceKey.ACTIVE})
    return key


def _proving_key(request) -> RunnerDeviceKey:
    """The box the request names (`X-Canopy-Runner`, one the caller owns) proves with its
    own key; a request that names none is the caller at their own laptop."""
    named = request.META.get("HTTP_X_CANOPY_RUNNER", "")
    if named:
        try:
            box = Runner.objects.filter(pk=named, owner=request.user).first()
        except (ValueError, ValidationError):
            box = None
        if box is not None:
            return _key_on(box)
    laptop, _ = Runner.objects.get_or_create(
        name=f"laptop-{request.user.pk}", owner=request.user,
        defaults={"kind": Runner.EMDASH, "status": Runner.ONLINE,
                  "last_heartbeat_at": timezone.now(), "capabilities": {}})
    return _key_on(laptop)


def accept_device_proof(monkeypatch, runner: Runner | None = None) -> None:
    """Every resolve carries a proof from an ACTIVE key whose person counts as a vault
    member. With a `runner`, it is always that box's key; without one, it is the key of the
    box the request names, else of the caller's own laptop (so an owner or admin resolving
    by hand reads as an admin)."""
    fixed = _key_on(runner) if runner is not None else None
    monkeypatch.setattr(credential_resolve, "device_key_from_proof",
                        lambda request, agent: (fixed or _proving_key(request), None, None))
    monkeypatch.setattr(credential_resolve.vault_members, "device_works_for_a_member", lambda agent, k: True)
