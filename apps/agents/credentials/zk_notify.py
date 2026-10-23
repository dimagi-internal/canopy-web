"""Tell the people who hold an agent's vault key that someone is waiting for it
(Canopy Vault Phase 2, canopy-web#1550).

Under zero-knowledge, canopy-web cannot hand the vault key to a newly approved
device or a new admin — only a person who holds it can, in their browser. So
when a pending wrap target appears (`zk.pending_targets`: an ACTIVE device key
on a runner the agent routes to, or an admin with a vault keyset, with no wrap
at the current kv), every current holder — each person with a member wrap at
that kv — gets an email and a Web Push, the same channel as the vault's read
alerts (`vault_alerts`), deep-linking to the agent's Zero-knowledge section,
where granting is one passkey touch.

* After a grace period (GRACE): approving a device usually grants it in the
  same click (`approveAndGrant`), and a keeper (a canopy runner that holds the
  key, `canopy vault zk keep`) grants every endorsed target within a minute or
  two — neither may mail anyone. The email is the fallback for what no machine
  could pass on.
* Agent vaults and workspace vaults alike (a workspace vault's link goes to its own
  page, `/w/<ws>/vaults/<id>`).
* Once per target (`ZkGrantNotice`, unique per vault/kind/target/kv); the row
  is closed when the target is wrapped or stops qualifying.
* Swept on the off-request clock (apps/common/ticks.py), at most once a minute.

BEST-EFFORT: nothing here may cost a runner its heartbeat; every entry point
swallows its own failures.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from html import escape

from django.core.cache import cache
from django.utils import timezone

from apps.passkeys.models import UserVaultKeyset
from apps.push.services import send_to_user
from apps.workspaces import links as wslinks

from ..models import Agent, AgentVaultWrap, Vault, VaultKeyWrap, ZkGrantNotice
from . import vault_members, workspace_vaults, zk

logger = logging.getLogger(__name__)

GRACE = dt.timedelta(minutes=15)
SWEEP_EVERY_S = 60
_SWEEP_KEY = "zk-grant-notify:sweep"


def maybe_sweep(now=None) -> None:
    """Sweep at most once a minute across the fleet. Never raises."""
    try:
        if cache.add(_SWEEP_KEY, 1, timeout=SWEEP_EVERY_S):
            sweep(now)
    except Exception:  # noqa: BLE001
        logger.exception("zk grant notify: sweep failed")


@dataclass
class _Waiting:
    """One vault the sweep looks at: an agent's own, or a workspace vault."""

    what: str                      # "ace" / "the Shared vault"
    kv: int
    link: str
    where: dict                    # how its ZkGrantNotice rows are keyed: {"agent": a} | {"vault": v}
    pending: Callable[[], list]
    holders: Callable[[], list]


def _vaults():
    for agent in Agent.objects.filter(zk_enabled=True, zk_kv__gte=1).select_related("workspace"):
        yield _Waiting(agent.slug, agent.zk_kv, _link(agent), {"agent": agent},
                       partial(zk.pending_targets, agent), partial(holders, agent))
    for v in Vault.objects.filter(kind=Vault.WORKSPACE, kv__gte=1).select_related("workspace"):
        yield _Waiting(f"the {v.name} vault", v.kv,
                       wslinks.scoped_url(v.workspace, f"/vaults/{v.pk}"), {"vault": v},
                       partial(workspace_vaults.pending_targets, v), partial(workspace_holders, v))


def sweep(now=None) -> int:
    """Record, notify and clear pending-grant notices, for agent vaults and workspace
    vaults alike. Returns how many were notified this pass."""

    now = now or timezone.now()
    told = 0
    for w in _vaults():
        try:
            pending = {(t["kind"], t["target"]): t for t in w.pending()}
        except Exception:  # noqa: BLE001
            logger.exception("zk grant notify: pending targets failed for %s", w.what)
            continue
        for notice in ZkGrantNotice.objects.filter(**w.where, cleared_at__isnull=True):
            if notice.kv != w.kv or (notice.kind, notice.target) not in pending:
                notice.cleared_at = now
                notice.save(update_fields=["cleared_at"])
        # A person who started over with a new key waits for a PERSON: no keeper grants
        # a changed key (it is what a swapped one looks like), so tell the holders now.
        restarted = dict(UserVaultKeyset.objects.filter(
            user_id__in=[int(tg) for (k, tg) in pending if k == "member" and tg.isdigit()],
            replaced_at__isnull=False).values_list("user_id", "replaced_at"))
        for (kind, target), t in pending.items():
            notice, _ = ZkGrantNotice.objects.get_or_create(
                **w.where, kind=kind, target=target, kv=w.kv,
                defaults={"first_seen_at": now, "label": _label(t)[:300]},
            )
            new_key = restarted.get(int(target)) if kind == "member" and target.isdigit() else None
            if notice.notified_at or notice.cleared_at or (new_key is None and now - notice.first_seen_at < GRACE):
                continue
            notice.notified = _notify(w, notice, new_key=new_key)
            notice.notified_at = now
            notice.save(update_fields=["notified", "notified_at"])
            told += 1
    return told


def _label(t: dict) -> str:
    if t["kind"] == "device":
        return f"{t.get('runner_name') or 'a runner'} · {t.get('label') or t.get('device_kind') or 'device'}"
    return t.get("email") or "a new member"


def workspace_holders(vault) -> list:
    """`holders`, for a workspace vault."""

    ids = vault_members.member_ids(vault)
    return [w.user for w in VaultKeyWrap.objects.filter(vault=vault, kv=vault.kv, kind="member", user_id__in=ids)
            .select_related("user") if w.user.is_active]


def holders(agent) -> list:
    """Everyone who can grant: a member wrap at the current kv, still active, and
    still a member of the vault — a copy alone makes nobody a holder."""

    ids = vault_members.agent_member_ids(agent)
    return [w.user for w in AgentVaultWrap.objects.filter(agent=agent, kv=agent.zk_kv, kind="member",
                                                          user_id__in=ids)
            .select_related("user") if w.user.is_active]


def _link(agent) -> str:

    return wslinks.scoped_url(agent.workspace, f"/agents/{agent.slug}/settings/security#cred-zk")


def _notify(w: _Waiting, notice, *, new_key=None) -> dict:
    """Email + Web Push to each person who holds the vault key. Never raises.
    `new_key`: when the person started over with a new vault key — the grant is then
    also the confirmation that the new key is theirs."""
    from django.core.mail import EmailMultiAlternatives

    result: dict = {"emailed": [], "failed": [], "not_configured": False, "pushed": 0}
    try:
        what = (f"The machine {notice.label}" if notice.kind == "device"
                else f"{notice.label}, a member of {w.what}'s vault,")
        if new_key is not None:
            when = timezone.localtime(new_key).strftime("%b %-d")
            subject = f"Canopy vault · {notice.label} has a new vault key and is waiting for {w.what}'s key"
            detail = (f"{notice.label} started over with a new vault key on {when}, so they need {w.what}'s key "
                      f"again. Runners won't pass it on to a changed key by themselves. Check with them that "
                      f"it was them, then grant it with one passkey touch; that also confirms the new key.")
        else:
            subject = f"Canopy vault · {notice.label} is waiting for {w.what}'s key"
            detail = (f"{what} can't open {w.what}'s secrets yet, and no machine holding the key has passed "
                      f"it on. Only someone who holds the key can: grant it with one passkey touch.")
        text = (f"{detail}\n\n{w.link}\n\nYou are getting this because you hold {w.what}'s key. "
                f"One notice per machine or person.\n")
        html = (f"<p>{escape(detail)}</p><p><a href=\"{escape(w.link)}\">Grant it</a></p>"
                f"<p style=\"color:#78716c;font-size:13px\">You are getting this because you hold "
                f"{escape(w.what)}&rsquo;s key. One notice per machine or person.</p>")
        rel = w.link.split("://", 1)[-1]
        rel = rel[rel.find("/"):] if "/" in rel else "/"
        for person in w.holders():
            if person.email:
                message = EmailMultiAlternatives(subject=subject, body=text, to=[person.email])
                message.attach_alternative(html, "text/html")
                try:
                    if message.send():
                        result["emailed"].append(person.email)
                    else:
                        result["not_configured"] = True
                except Exception:  # noqa: BLE001
                    logger.exception("zk grant notice email failed: to=%s", person.email)
                    result["failed"].append(person.email)
            try:
                result["pushed"] += send_to_user(person, f"Vault key wanted · {w.what}", detail[:180], rel)
            except Exception:  # noqa: BLE001
                logger.exception("zk grant notice push failed: user=%s", person.pk)
    except Exception:  # noqa: BLE001
        logger.exception("zk grant notice failed for %s", w.what)
        result["error"] = True
    return result
