"""The keeper: a runner that holds an agent's vault key passes it on, with nobody asked
(spec 2026-10-22-vault-access-follows-canopy-access-design.md §1).

Under zero-knowledge only a holder of the vault key can give a copy to someone new.
A runner that holds one (its device wrap at the current kv) is a holder, and it is
online, so it can do what the "Give access" email asked a person to do. The box does
the crypto and the TRUST CHECK (canopy `vault zk keep`: a member's key is pinned on
first sight, a machine needs an endorsement from a person it trusts); canopy-web only
says who is waiting and stores what comes back.

What canopy-web enforces here, so a keeper adds no authority of its own:

* it lists, and accepts wraps for, only agents whose CURRENT vault key this box holds;
* a wrap is accepted only for a target that is pending — eligible now
  (`zk.pending_targets`: a vault member, or a machine whose person is one) and holding
  no copy yet — so a keeper can never replace a working copy with a bad one;
* everything else is `zk.add_wraps`, the same check a person's grant goes through.
"""
from __future__ import annotations

from apps.harness.models import RunnerDeviceKey

from ..models import Agent, AgentVaultWrap
from . import zk, zk_endorse


def _held(runner) -> list[AgentVaultWrap]:
    """This box's device wraps at each zero-knowledge agent's current kv."""
    return [w for w in (AgentVaultWrap.objects
                        .filter(kind=AgentVaultWrap.DEVICE, device_key__runner=runner,
                                device_key__status=RunnerDeviceKey.ACTIVE, agent__zk_enabled=True)
                        .select_related("agent", "device_key"))
            if w.kv == w.agent.zk_kv]


def work(runner) -> list[dict]:
    """For each agent this box holds the current vault key of AND someone is waiting
    for: what a keeper needs to grant — its own wrap, the pending targets, and the
    endorsements it checks them against."""
    out = []
    for w in _held(runner):
        agent = w.agent
        pending = zk.pending_targets(agent)
        if not pending:
            continue
        out.append({"agent": agent.slug, "agent_id": str(agent.pk), "kv": agent.zk_kv,
                    "device_key": str(w.device_key_id), "wrap": w.wrap, "pending": pending,
                    "endorsements": zk_endorse.endorsements(agent)})
    return out


def grant(runner, slug: str, *, kv: int, wraps: list) -> int:
    """Store a keeper's wraps for `slug`. Refused unless this box holds the current
    vault key and every target is pending (eligible, with no copy yet)."""
    agent = Agent.objects.filter(slug=slug).first()
    if agent is None or not any(w.agent_id == agent.pk for w in _held(runner)):
        raise zk.ZkError(403, "zk_not_a_holder",
                         f"this runner holds no copy of {slug}'s current vault key")
    pending = {(t["kind"], t["target"]) for t in zk.pending_targets(agent)}
    for i, item in enumerate(wraps):
        if (item.kind, str(item.target)) not in pending:
            raise zk.ZkError(409, "zk_not_pending",
                             f"wraps[{i}]: {item.kind} {item.target} is not waiting for a copy — a "
                             "keeper only adds copies, never replaces one")
    return zk.add_wraps(agent, kv=kv, wraps=wraps, user=runner.owner)
