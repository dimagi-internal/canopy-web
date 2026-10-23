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
* Once per target (`ZkGrantNotice`, unique per agent/kind/target/kv); the row
  is closed when the target is wrapped or stops qualifying.
* Swept on the off-request clock (apps/common/ticks.py), at most once a minute.

BEST-EFFORT: nothing here may cost a runner its heartbeat; every entry point
swallows its own failures.
"""
from __future__ import annotations

import datetime as dt
import logging
from html import escape

from django.core.cache import cache
from django.utils import timezone

from apps.push.services import send_to_user
from apps.workspaces import links as wslinks

from . import zk
from ..models import Agent, AgentVaultWrap, ZkGrantNotice

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


def sweep(now=None) -> int:
    """Record, notify and clear pending-grant notices. Returns how many were
    notified this pass."""

    now = now or timezone.now()
    told = 0
    for agent in Agent.objects.filter(zk_enabled=True, zk_kv__gte=1):
        try:
            pending = {(t["kind"], t["target"]): t for t in zk.pending_targets(agent)}
        except Exception:  # noqa: BLE001
            logger.exception("zk grant notify: pending targets failed for %s", agent.slug)
            continue
        for notice in ZkGrantNotice.objects.filter(agent=agent, cleared_at__isnull=True):
            if notice.kv != agent.zk_kv or (notice.kind, notice.target) not in pending:
                notice.cleared_at = now
                notice.save(update_fields=["cleared_at"])
        for (kind, target), t in pending.items():
            notice, _ = ZkGrantNotice.objects.get_or_create(
                agent=agent, kind=kind, target=target, kv=agent.zk_kv,
                defaults={"first_seen_at": now, "label": _label(t)[:300]},
            )
            if notice.notified_at or notice.cleared_at or now - notice.first_seen_at < GRACE:
                continue
            notice.notified = _notify(agent, notice)
            notice.notified_at = now
            notice.save(update_fields=["notified", "notified_at"])
            told += 1
    return told


def _label(t: dict) -> str:
    if t["kind"] == "device":
        return f"{t.get('runner_name') or 'a runner'} · {t.get('label') or t.get('device_kind') or 'device'}"
    return t.get("email") or "a new admin"


def holders(agent) -> list:
    """Everyone who can grant: a member wrap at the current kv, still active."""

    return [w.user for w in AgentVaultWrap.objects.filter(agent=agent, kv=agent.zk_kv, kind="member")
            .select_related("user") if w.user_id and w.user.is_active]


def _link(agent) -> str:

    return wslinks.scoped_url(agent.workspace, f"/agents/{agent.slug}/settings/security#cred-zk")


def _notify(agent, notice) -> dict:
    """Email + Web Push to each holder. Never raises."""
    from django.core.mail import EmailMultiAlternatives

    result: dict = {"emailed": [], "failed": [], "not_configured": False, "pushed": 0}
    try:
        name = agent.name or agent.slug
        link = _link(agent)
        what = (f"The device {notice.label}" if notice.kind == "device"
                else f"{notice.label}, a new admin of {agent.slug},")
        subject = f"Canopy vault · {name}: {notice.label} is waiting for the vault key"
        detail = (f"{what} is approved but can't read {name}'s secrets yet: zero-knowledge means only "
                  f"someone who holds the vault key can pass it on. Grant it with one passkey touch.")
        text = (f"{detail}\n\n{link}\n\nYou are getting this because you hold {agent.slug}'s vault key. "
                f"One notice per device or person.\n")
        html = (f"<p>{escape(detail)}</p><p><a href=\"{escape(link)}\">Grant it on {escape(agent.slug)}&rsquo;s "
                f"settings</a></p><p style=\"color:#78716c;font-size:13px\">You are getting this because you hold "
                f"{escape(agent.slug)}&rsquo;s vault key. One notice per device or person.</p>")
        rel = link.split("://", 1)[-1]
        rel = rel[rel.find("/"):] if "/" in rel else "/"
        for person in holders(agent):
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

                result["pushed"] += send_to_user(person, f"Vault key wanted · {agent.slug}", detail[:180], rel)
            except Exception:  # noqa: BLE001
                logger.exception("zk grant notice push failed: user=%s", person.pk)
    except Exception:  # noqa: BLE001
        logger.exception("zk grant notice failed for %s", agent.slug)
        result["error"] = True
    return result
