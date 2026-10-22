"""Sender notices canopy-web could not send, sent by the agent's runner instead.

canopy-web sends a rejected turn's automatic reply itself only when it holds a
token it can refresh (`gmail.py`): one minted on its own Google client. Most
agents' mailboxes are on the fleet's desktop `canopy` client, so for them the
notice is left PENDING (canopy-web#1416, option b of #1400). The runner that
reads the agent's mailbox already holds a client that can send from it:

1. On its mailbox poll it CLAIMS the pending notices for the agents it reads
   (`claim`). A claim is a lease: a runner that dies mid-send loses it after
   `LEASE`, and the notice goes to whoever polls next.
2. It sends the SAME fixed text the server would have, threaded on the same
   message, with `Auto-Submitted: auto-replied`, refusing a message with
   automation headers (`runner/canopy_runner/canopy_runner/admission_notices.py`).
3. It REPORTS the outcome (`report`): sent and skipped are final; a failure
   keeps the notice pending, so the next lease retries it.

A notice no runner sends within `EXPIRES` ends `failed`, saying why — by then
an automatic reply would arrive a day late, and "never silently failed" means
the reason is on the row, not that the notice waits forever. Expiry runs on the
off-request clock (`maybe_expire`, apps/common/ticks.py), so it happens even when
no runner reads the mailbox at all.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import AdmissionNotice

logger = logging.getLogger(__name__)

#: How long a runner holds a claimed notice before another may take it.
LEASE = dt.timedelta(minutes=15)
#: How long a notice may wait for a runner before it is given up on.
EXPIRES = dt.timedelta(hours=24)

#: What a runner may report. A failure is not final: see `report`.
OUTCOMES = (AdmissionNotice.SENT, AdmissionNotice.SKIPPED, AdmissionNotice.FAILED)


def _pending():
    return AdmissionNotice.objects.filter(kind=AdmissionNotice.SENDER, status=AdmissionNotice.PENDING)


def claim(runner, mailboxes: dict[str, str]) -> list[AdmissionNotice]:
    """Lease this runner the pending notices of the agents it reads.

    `mailboxes` is {agent slug: the address this runner reads for it}. A notice
    is handed over only when the runner may hold the agent (its owner is one of
    the agent's admins — the same rule as claiming the agent's turns), the agent
    is in the runner's tenant, and the address is the notice's mailbox: a reply
    must come from the mailbox the sender wrote to.
    """
    if not mailboxes:
        return []
    from apps.agents.services import runner_may_hold_agent
    from apps.harness import routing

    now = timezone.now()
    expire(now)
    tenant = routing.runner_tenant_slugs(runner)
    wanted = {slug: (addr or "").strip().lower() for slug, addr in mailboxes.items()}
    out: list[AdmissionNotice] = []
    with transaction.atomic():
        rows = (_pending()
                .filter(agent__slug__in=list(wanted), agent__workspace_id__in=tenant)
                .filter(Q(claimed_at__isnull=True) | Q(claimed_at__lt=now - LEASE))
                .select_related("agent")
                .select_for_update(skip_locked=True, of=("self",))
                .order_by("created_at"))
        for notice in rows:
            agent = notice.agent
            if not runner_may_hold_agent(runner, agent):
                continue
            if notice.mailbox and notice.mailbox.lower() != wanted.get(agent.slug):
                continue
            notice.claimed_by, notice.claimed_at = runner, now
            notice.save(update_fields=["claimed_by", "claimed_at"])
            out.append(notice)
    return out


def notice_out(notice: AdmissionNotice) -> dict:
    agent = notice.agent
    return {
        "id": notice.pk, "agent": agent.slug, "agent_name": agent.name,
        "mailbox": notice.mailbox, "thread_id": notice.thread_id,
        "message_id": notice.message_id, "to": notice.recipient,
        "subject": notice.subject, "body": notice.body,
    }


def report(runner, notice_id: int, outcome: str, detail: str) -> bool:
    """Record what the runner did with a notice it holds. False when it does
    not hold it (never claimed, lease taken by another runner, already final).

    `sent` and `skipped` (the runner found automation headers, or the thread
    already carries the reply) are final. `failed` is not: the notice stays
    pending with the error recorded and the lease left to run out, so the next
    poll after `LEASE` retries it — until `EXPIRES` gives up on it.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
    detail = (detail or "").strip()[:1000]
    with transaction.atomic():
        notice = _pending().select_for_update().filter(pk=notice_id, claimed_by=runner).first()
        if notice is None:
            return False
        line = f"runner {runner.name}: {outcome}" + (f" — {detail}" if detail else "")
        notice.detail = f"{notice.detail}\n{line}".strip()
        fields = ["detail"]
        if outcome != AdmissionNotice.FAILED:
            notice.status = outcome
            fields.append("status")
        notice.save(update_fields=fields)
    return True


def expire(now=None) -> int:
    """Give up on pending notices older than `EXPIRES`, saying why."""
    now = now or timezone.now()
    n = 0
    for notice in _pending().filter(created_at__lt=now - EXPIRES):
        tried = "a runner tried and failed" if notice.claimed_by_id else "no runner that reads the mailbox claimed it"
        notice.status = AdmissionNotice.FAILED
        notice.detail = (f"{notice.detail}\nnot sent: {tried} within "
                         f"{int(EXPIRES.total_seconds() // 3600)} hours, and a reply now would be "
                         "a day late").strip()
        notice.save(update_fields=["status", "detail"])
        n += 1
    return n


_last_expiry: dt.datetime | None = None


def maybe_expire(now) -> None:
    """The off-request clock's call: at most once a minute, and never raises."""
    global _last_expiry
    if _last_expiry is not None and now - _last_expiry < dt.timedelta(minutes=1):
        return
    _last_expiry = now
    try:
        expire(now)
    except Exception:  # noqa: BLE001 — a notice must never stop the other jobs
        logger.exception("admission: expiring pending sender notices failed")
