"""Tell the people who hold an agent's vault key that someone is waiting for it
(Canopy Vault Phase 2, canopy-web#1550).

Under zero-knowledge, canopy-web cannot hand the vault key to a newly approved
device or a new admin — only a person who holds it can, in their browser. So
when a pending wrap target appears (`zk.pending_targets`: an ACTIVE device key
on a runner the agent routes to, or an admin with a vault keyset, with no wrap
at the current kv), every current holder who may grant it — an owner or admin
of the vault with a member wrap at that kv (a member's copy opens the vault but
grants nothing) — gets an email and a Web Push, the same channel as the vault's
read alerts (`vault_alerts`), deep-linking to the agent's Zero-knowledge section,
where granting is one passkey touch.

* It is about a PERSON, never a bare machine: the admin is asked whether they
  would let this person in, which they can answer. Three cases — someone joining
  for the first time, someone with a new vault key (a changed key is what a swapped
  one looks like, so no keeper grants it), or someone the key has not reached since
  it was rotated. A machine is named as its person's ("Sunaina's machine
  DIM-SI-W021"), and is not mentioned at all while its person is waiting too:
  granting the person is the question, and one Grant pending covers both.

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

from apps.harness.models import RunnerDeviceKey
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
    had_copy: Callable[[int], bool]    # did this person ever hold a copy (any kv)?


def _vaults():
    for agent in Agent.objects.filter(zk_enabled=True, zk_kv__gte=1).select_related("workspace"):
        yield _Waiting(agent.slug, agent.zk_kv, _link(agent), {"agent": agent},
                       partial(zk.pending_targets, agent), partial(holders, agent),
                       lambda uid, a=agent: AgentVaultWrap.objects.filter(
                           agent=a, kind="member", user_id=uid).exists())
    for v in Vault.objects.filter(kind=Vault.WORKSPACE, kv__gte=1).select_related("workspace"):
        yield _Waiting(f"the {v.name} vault", v.kv,
                       wslinks.scoped_url(v.workspace, f"/vaults/{v.pk}"), {"vault": v},
                       partial(workspace_vaults.pending_targets, v), partial(workspace_holders, v),
                       lambda uid, v=v: VaultKeyWrap.objects.filter(vault=v, kind="member", user_id=uid).exists())


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
        waiting_people = {int(tg) for (k, tg) in pending if k == "member" and tg.isdigit()}
        for (kind, target), t in pending.items():
            notice, _ = ZkGrantNotice.objects.get_or_create(
                **w.where, kind=kind, target=target, kv=w.kv,
                defaults={"first_seen_at": now, "label": _label(t)[:300]},
            )
            new_key = restarted.get(int(target)) if kind == "member" and target.isdigit() else None
            if notice.notified_at or notice.cleared_at or (new_key is None and now - notice.first_seen_at < GRACE):
                continue
            if kind == "member":
                why = _NEW_KEY if new_key is not None else (_ROTATED if w.had_copy(int(target)) else _FIRST_TIME)
                person, machine = notice.label, ""
            else:
                person_id, person = _person_of(target)
                if person_id in waiting_people:
                    # Its person is waiting too: their notice is the question, and one
                    # Grant pending covers both. Never ask about the machine on its own.
                    notice.notified = {"folded_into_person": person}
                    notice.notified_at = now
                    notice.save(update_fields=["notified", "notified_at"])
                    continue
                why, machine = _MACHINE, t.get("runner_name") or notice.label
            notice.notified = _notify(w, notice, why=why, person=person, machine=machine, new_key=new_key)
            notice.notified_at = now
            notice.save(update_fields=["notified", "notified_at"])
            told += 1
    return told


def _label(t: dict) -> str:
    if t["kind"] == "device":
        return f"{t.get('runner_name') or 'a runner'} · {t.get('label') or t.get('device_kind') or 'device'}"
    return t.get("email") or "a new member"


def _admin_ids(vault) -> set[int]:
    """The vault's owners and admins: only they may grant (`api_credentials._zk_admin`)."""
    return {uid for uid, m in vault_members.members(vault).items() if vault_members.is_admin(m.role)}


def _person_of(device_pk: str) -> tuple[int | None, str]:
    """(user id, email) of the person a waiting machine works for — its runner's owner,
    else whoever approved it (`vault_members.person_of_device`)."""
    k = RunnerDeviceKey.objects.filter(pk=device_pk).select_related("runner__owner", "approved_by").first()
    if k is None:
        return None, ""
    uid = vault_members.person_of_device(k)
    user = k.runner.owner if k.runner.owner_id == uid else k.approved_by
    return uid, (user.email if user is not None else "")


def workspace_holders(vault) -> list:
    """`holders`, for a workspace vault."""

    ids = _admin_ids(vault)
    return [w.user for w in VaultKeyWrap.objects.filter(vault=vault, kv=vault.kv, kind="member", user_id__in=ids)
            .select_related("user") if w.user.is_active]


def holders(agent) -> list:
    """Everyone who can grant: a member wrap at the current kv, still active, and an
    owner or admin of the vault now — a copy alone makes nobody a holder, and a
    member's copy opens the vault but cannot grant (`api_credentials._zk_admin`)."""

    ids = _admin_ids(vault_members.vault_for_agent(agent))
    return [w.user for w in AgentVaultWrap.objects.filter(agent=agent, kv=agent.zk_kv, kind="member",
                                                          user_id__in=ids)
            .select_related("user") if w.user.is_active]


def _link(agent) -> str:

    return wslinks.scoped_url(agent.workspace, f"/agents/{agent.slug}/settings/security#cred-zk")


_FIRST_TIME, _NEW_KEY, _ROTATED, _MACHINE = "first_time", "new_key", "rotated", "machine"


def _message(w: _Waiting, why: str, person: str, machine: str, new_key) -> tuple[str, str]:
    """(subject, the paragraph that says what happened and what to do)."""
    who = person or "Someone"
    keepers = ("A keeper (a canopy runner that holds the key) normally hands it over within a "
               "couple of minutes; none has in 15 minutes, so most likely none is online.")
    if why == _NEW_KEY:
        when = timezone.localtime(new_key).strftime("%b %-d")
        return (f"Canopy vault · {who} has a new vault key — confirm it's them before granting {w.what}",
                f"{who} set up a new vault key on {when} and needs {w.what}'s key again. Keepers never "
                f"hand the key to a changed key by themselves, because a changed key is also what a "
                f"swapped one would look like. Check with {who} directly (Slack or a call, not a reply "
                f"to this email) that they did this, then grant it with one passkey touch; that also "
                f"confirms the new key, so keepers trust it from then on. If they didn't, don't grant.")
    if why == _FIRST_TIME:
        return (f"Canopy vault · {who} is joining {w.what}'s vault for the first time",
                f"{who} was given access to {w.what} and is waiting for its vault key for the first "
                f"time. {keepers} If you expected {who} to join, grant it with one passkey touch. If "
                f"you didn't, find out who added them before you grant anything.")
    if why == _ROTATED:
        return (f"Canopy vault · {who} is waiting for {w.what}'s new key",
                f"{w.what}'s vault key was rotated and the new one hasn't reached {who} yet. {keepers} "
                f"Nothing about {who} has changed: grant it with one passkey touch.")
    owner = f"{person}'s" if person else "A"
    return (f"Canopy vault · {owner} machine {machine} is waiting for {w.what}'s key",
            f"{owner} machine {machine} was approved{f' by {person}' if person else ''} but can't open "
            f"{w.what}'s secrets yet. {keepers} You don't need to recognise the machine: "
            f"{person or 'its owner'} approved it with their passkey. The question is only whether "
            f"you'd let {person or 'them'} read {w.what}'s secrets on it — if so, grant it with one "
            f"passkey touch.")


def _notify(w: _Waiting, notice, *, why: str, person: str = "", machine: str = "", new_key=None) -> dict:
    """Email + Web Push to each admin of the vault who holds its key. Never raises.
    `new_key`: when the person started over with a new vault key — the grant is then
    also the confirmation that the new key is theirs."""
    from django.core.mail import EmailMultiAlternatives

    result: dict = {"emailed": [], "failed": [], "not_configured": False, "pushed": 0, "why": why}
    try:
        subject, detail = _message(w, why, person, machine, new_key)
        footer = (f"You're getting this as an admin of {w.what}'s vault who holds its key — only an "
                  f"admin can let someone in. One notice per person or machine.")
        text = f"{detail}\n\n{w.link}\n\n{footer}\n"
        html = (f"<p>{escape(detail)}</p><p><a href=\"{escape(w.link)}\">Review and grant</a></p>"
                f"<p style=\"color:#78716c;font-size:13px\">{escape(footer)}</p>")
        rel = w.link.split("://", 1)[-1]
        rel = rel[rel.find("/"):] if "/" in rel else "/"
        for admin in w.holders():
            if admin.email:
                message = EmailMultiAlternatives(subject=subject, body=text, to=[admin.email])
                message.attach_alternative(html, "text/html")
                try:
                    if message.send():
                        result["emailed"].append(admin.email)
                    else:
                        result["not_configured"] = True
                except Exception:  # noqa: BLE001
                    logger.exception("zk grant notice email failed: user=%s", admin.pk)
                    result["failed"].append(admin.email)
            try:
                result["pushed"] += send_to_user(admin, subject.removeprefix("Canopy vault · "), detail[:180], rel)
            except Exception:  # noqa: BLE001
                logger.exception("zk grant notice push failed: user=%s", admin.pk)
    except Exception:  # noqa: BLE001
        logger.exception("zk grant notice failed for %s", w.what)
        result["error"] = True
    return result
