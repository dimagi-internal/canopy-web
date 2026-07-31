"""Request-free service layer for inbound push.

Ringing a runner is best-effort by design: the frame is a latency optimization
over the runner's own 300s poll, exactly like the `wake` frame is over its 5s
claim poll. A frame we fail to deliver costs latency, never correctness.

What is NOT best-effort is SAYING SO. Every outcome writes an event, including
the ones that used to be silent — a push for a mailbox nobody owns, a push with
no online runner to ring. "It'll get picked up by the next poll" is the failure
mode this app exists to make visible, not a defence.
"""
from __future__ import annotations

import datetime as dt

from django.utils import timezone

from apps.events import services as events
from apps.harness import services as harness
from apps.harness.models import Runner, Turn
from apps.inbound.models import InboundMailbox
from apps.realtime.groups import publish, runner_group

SOURCE = "inbound.gmail"

#: How close to expiry a watch may drift before we say so. The runner re-arms at
#: 24h; warning at the same threshold means a warn row appears exactly when the
#: re-arm should have happened and didn't.
WATCH_WARN_WINDOW = dt.timedelta(hours=24)


def _record(workspace, **kw) -> None:
    events.record([{"source": SOURCE, **kw}], workspace=workspace)


def resolve_mailbox(address: str) -> InboundMailbox | None:
    return (
        InboundMailbox.objects.select_related("agent")
        .filter(address__iexact=(address or "").strip(), enabled=True)
        .first()
    )


def online_runners_for(mailbox: InboundMailbox) -> list[Runner]:
    """Every ONLINE runner that could claim this mailbox's turn, best rank first.

    Composed with `harness.assignment_rows_for(..., ORIGIN_EMAIL, ...)` — the same
    helper `claim_next_turn` and `unclaimable_queued_turns` use — rather than
    reading `RunnerAssignment` directly. Mail becomes an `email`-origin turn, so a
    source rule naming a priority runner for `email` (or a STRICT one naming only
    it) must be honoured here too. Re-deriving the list would let the doorbell ring
    a runner that routing has already decided will never claim, which is precisely
    the disagreement the shared-helper rule exists to prevent.

    Ring them ALL rather than just the top rank. Two runners reading the same
    mailbox is harmless — the enqueue is idempotent per (thread, messageCount), so
    the second read collapses server-side — and ringing only the best rank would
    land nothing in exactly the case a lower rank is about to claim anyway (the
    cascade grace). Cheap redundancy beats a clever guess. Disabled rows are
    already filtered by `load_assignment_rows`.
    """
    defaults, priorities = harness.load_assignment_rows([mailbox.agent_id])
    rows = harness.assignment_rows_for(
        mailbox.agent_id, Turn.ORIGIN_EMAIL, defaults, priorities
    )
    # `is_available`, not `live_status == ONLINE`: it is the cascade's own
    # availability probe (online AND self-reported ready), so "who might claim
    # this" is answered the same way here as at claim time. It also picks up the
    # pause for free — `live_status` serves PAUSED, which is exactly the design
    # that stops a new caller forgetting to check it.
    return [r for _rank, r in rows if r.is_available]


def ring(mailbox: InboundMailbox) -> list[Runner]:
    """Tell every eligible runner to check this mailbox now. Returns who we rang."""
    runners = online_runners_for(mailbox)
    for runner in runners:
        publish(
            runner_group(runner.id),
            {"type": "runner.check_inbox", "mailbox": mailbox.address},
        )
    return runners


def handle_push(address: str, history_id: str = "") -> dict:
    """A verified Gmail push arrived. Resolve it, ring, and log the outcome.

    Returns a small dict for the response body — the caller answers 200 for
    every branch, including the unresolvable ones, because a 4xx to Pub/Sub
    means REDELIVERY and a mailbox we do not own would then be retried forever.
    Refusing loudly in the log is the right answer; refusing over the wire just
    creates a retry storm.
    """
    mailbox = resolve_mailbox(address)
    if mailbox is None:
        # No tenant to attribute this to — an unknown mailbox belongs to no
        # agent by definition. Log it against the default workspace so it is
        # still visible; this is the one case with no better home.
        from apps.workspaces import services as wsvc

        home = wsvc.ensure_default_workspace()
        if home is not None:
            _record(
                home,
                kind="gmail.push.unknown_mailbox",
                level="warn",
                key=(address or "")[:200],
                summary=f"push for a mailbox with no InboundMailbox row: {address}",
                payload={"address": address},
            )
        return {"ok": False, "reason": "unknown_mailbox"}

    workspace = mailbox.agent.workspace
    InboundMailbox.objects.filter(pk=mailbox.pk).update(last_push_at=timezone.now())

    runners = ring(mailbox)
    if not runners:
        # Loud, not deferred. The mail will still be found by the 300s poll when
        # a runner returns, but "nobody was listening" is exactly the condition
        # that makes push look broken when it isn't.
        _record(
            workspace,
            kind="gmail.push.no_runner",
            level="warn",
            key=mailbox.address,
            summary=f"push for {mailbox.address} but no online runner is assigned to "
                    f"{mailbox.agent.slug}",
            payload={"address": mailbox.address, "agent": mailbox.agent.slug},
        )
        return {"ok": False, "reason": "no_runner"}

    _record(
        workspace,
        kind="gmail.push",
        level="info",
        key="",  # an action, not a fault — every push is its own row
        summary=f"rang {len(runners)} runner(s) for {mailbox.address}",
        payload={
            "address": mailbox.address,
            "agent": mailbox.agent.slug,
            "history_id": history_id,
            "runners": [r.name for r in runners],
        },
    )
    return {"ok": True, "rang": [r.name for r in runners]}


def note_watch_state(mailbox: InboundMailbox, expires_at: dt.datetime | None) -> None:
    """Record what the runner says about this mailbox's Gmail watch.

    A watch Google will not renew is the silent failure this whole design is
    guarding against: it lapses, push stops, and the only symptom is that email
    feels slow again.
    """
    InboundMailbox.objects.filter(pk=mailbox.pk).update(watch_expires_at=expires_at)
    if expires_at is None:
        return
    workspace = mailbox.agent.workspace
    now = timezone.now()
    if expires_at <= now:
        _record(
            workspace,
            kind="gmail.watch.expired",
            level="error",
            key=mailbox.address,
            summary=f"Gmail watch for {mailbox.address} expired at {expires_at.isoformat()}",
            payload={"address": mailbox.address, "expires_at": expires_at.isoformat()},
        )
    elif expires_at - now <= WATCH_WARN_WINDOW:
        _record(
            workspace,
            kind="gmail.watch.expiring",
            level="warn",
            key=mailbox.address,
            summary=f"Gmail watch for {mailbox.address} expires {expires_at.isoformat()}",
            payload={"address": mailbox.address, "expires_at": expires_at.isoformat()},
        )
