"""Read-anomaly alerts on an agent's secrets (Canopy Vault Phase 1b, canopy-web#1549).

When an agent resolves its secrets from canopy-web, `/credentials/resolve` is
the only door to their values, so it is where a misuse shows. Four things are
worth a person's attention, and each is told to the agent's OWNER (or, with no
owner, its explicit admins) the way canopy tells an admin about an access
request: an email and a Web Push, each linking to the agent's Credentials.

* `no-turn` — a read by a BOX (resolve's `via == "runner"`: the caller pairs a
  runner this agent routes to AND the request names that box — a device proof
  or an owned `X-Canopy-Runner`; a bare token names no box and is an admin read,
  canopy-web#1563) that names no `X-Canopy-Turn-Id`. The issue's
  wording is "not from an owner or admin at a browser"; read against the code:
  every resolve caller is already an agent admin (`caller_runs_agent` requires
  it), resolve is bearer-only, and the owner/admin-at-a-browser read is a
  Reveal, which never comes through resolve. So the two non-browser readers
  are a box (should be inside a turn) and an admin resolving by hand for their
  own Claude Code session (`via == "admin"`, canopy#850 — expected without a
  turn, and alerting on it would email the owner about themselves hourly).
* `undeclared-ref` — a read that asks for, or is handed, a ref the agent's
  runtime.yaml does not declare.
* `burst` — more than CANOPY_VAULT_READ_BURST_LIMIT resolves (default 50) of one
  agent inside a 5-minute window. Counted per resolve request, not per ref: one
  unscoped resolve of ACE hands out 45 refs and is one read.
* `device-proof` — a resolve refused for a bad or missing device proof.

Debounced to one alert per kind per agent per hour: a repeat inside the hour
bumps the open alert's `occurrences` and tells no one again. Every alert is an
`AgentCredentialAlert` row, shown beside the credential read history, and an
`agent.credentials.alert` event.

BEST-EFFORT throughout: nothing here may deny a runner its secrets or turn a
401 into a 500, so every entry point swallows its own failures.
"""
from __future__ import annotations

import datetime as dt
import logging
from html import escape

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

DEBOUNCE = dt.timedelta(hours=1)
BURST_WINDOW_S = 300


def _burst_limit() -> int:
    return int(getattr(settings, "CANOPY_VAULT_READ_BURST_LIMIT", 50) or 50)


def _applies(agent) -> bool:
    # A zero-knowledge agent's values come from canopy-web whatever the source says.
    return agent.credential_source == agent.CANOPY_WEB or agent.zk_enabled


def after_read(agent, request, *, via: str, turn_id: str, asked: set[str] | None,
               handed: list[str], runner=None) -> list:
    """Check one SUCCESSFUL resolve. `via` is resolve's principal (`runner` or
    `admin`), `asked` the `?refs=` scope (None when unscoped), `handed` the
    credential refs whose values went out. Returns the alerts raised (new rows
    only)."""
    raised = []
    try:
        if not _applies(agent):
            return raised
        from . import services

        user = request.user
        if not turn_id and via == "runner":
            box = f" on {runner.name}" if runner is not None else ""
            raised.append(raise_alert(
                agent, "no-turn", user=user, runner=runner,
                detail=f"a box{box} ({_who(user)}) read {agent.slug}'s secrets outside a turn "
                       "(no X-Canopy-Turn-Id)",
                refs=sorted(handed)))
        declared = services.declared_credential_names(agent)
        undeclared = sorted(((asked or set()) | set(handed)) - declared - _EXTRAS)
        if undeclared:
            raised.append(raise_alert(
                agent, "undeclared-ref", user=user, runner=runner, turn_id=turn_id,
                detail=f"{_who(user)} asked for refs {agent.slug} does not declare: "
                       + ", ".join(undeclared)[:300],
                refs=undeclared))
        count = _count_read(agent)
        if count > _burst_limit():
            raised.append(raise_alert(
                agent, "burst", user=user, runner=runner, turn_id=turn_id,
                detail=f"{count} reads of {agent.slug}'s secrets in under 5 minutes "
                       f"(limit {_burst_limit()})"))
    except Exception:  # noqa: BLE001 - an alert hiccup must not deny a runner its secrets
        logger.exception("vault read-anomaly check failed for %s", agent.slug)
    return [a for a in raised if a is not None]


def device_proof_refused(agent, request, *, reason: str, runner=None):
    """A resolve answered 401 `device_proof_required`."""
    try:
        if not _applies(agent):
            return None
        return raise_alert(
            agent, "device-proof", user=request.user, runner=runner,
            turn_id=request.META.get("HTTP_X_CANOPY_TURN_ID", "")[:120],
            detail=f"resolve by {_who(request.user)} refused: {reason}"[:500])
    except Exception:  # noqa: BLE001
        logger.exception("vault device-proof alert failed for %s", agent.slug)
        return None


#: The resolve extras (RESOLVE_SECRET_EXTRAS in api.py) are fields, not refs a
#: runtime.yaml declares, so naming them is never "undeclared".
_EXTRAS = {"op_sa_token", "shared_op_sa_token", "github_token", "salesforce_creds"}


def _count_read(agent) -> int:
    """This resolve's place in the agent's current 5-minute window."""
    window = int(timezone.now().timestamp() // BURST_WINDOW_S)
    key = f"vault-reads:{agent.pk}:{window}"
    cache.add(key, 0, timeout=BURST_WINDOW_S * 2)
    try:
        return int(cache.incr(key))
    except ValueError:  # evicted between add and incr
        cache.set(key, 1, timeout=BURST_WINDOW_S * 2)
        return 1


def raise_alert(agent, kind: str, *, detail: str, refs=(), user=None, runner=None,
                turn_id: str = ""):
    """Record (and, unless one of this kind is open this hour, notify) an alert.
    Returns the new row, or None when it only counted against an open one."""
    from .models import Agent, AgentCredentialAlert

    now = timezone.now()
    with transaction.atomic():
        # Serialise per agent so two concurrent reads cannot both open an alert.
        Agent.objects.select_for_update().filter(pk=agent.pk).first()
        open_ = (AgentCredentialAlert.objects
                 .filter(agent=agent, kind=kind, created_at__gte=now - DEBOUNCE)
                 .order_by("-created_at").first())
        if open_ is not None:
            AgentCredentialAlert.objects.filter(pk=open_.pk).update(
                occurrences=F("occurrences") + 1, last_seen_at=now)
            return None
        alert = AgentCredentialAlert.objects.create(
            agent=agent, kind=kind, detail=detail[:500], refs=list(refs)[:100],
            user=user if getattr(user, "is_authenticated", False) else None,
            runner=runner, turn_id=(turn_id or "")[:120],
        )
    alert.notified = _notify(agent, alert)
    AgentCredentialAlert.objects.filter(pk=alert.pk).update(notified=alert.notified)
    _record_event(agent, alert)
    return alert


def _who(user) -> str:
    return getattr(user, "email", "") or "someone"


def recipients(agent) -> list:
    """The agent's owner; with none, its explicit admins (still members)."""
    if agent.owner_id and agent.owner.is_active:
        return [agent.owner]
    from .models import AgentAdmin

    return [g.user for g in AgentAdmin.objects.filter(agent=agent).select_related("user")
            if g.user.is_active and agent.is_admin(g.user)]


def _link(agent) -> str:
    from apps.workspaces import services as wsvc

    return wsvc.scoped_url(agent.workspace, f"/agents/{agent.slug}/settings#credentials")


#: What each kind watches for and what the owner should do, in plain words. The
#: detail line says what was seen; without this an owner cannot tell a misused
#: token from a config gap (canopy-web#1563).
_WHY = {
    "no-turn": ("Canopy watches for this because a runner box should only read an agent's "
                "secrets while it is running one of the agent's turns; a box reading them "
                "at any other time is what a stolen or misused runner token looks like. "
                "If you were not working on that box, pause it and revoke its token."),
    "undeclared-ref": ("Canopy watches for this because an agent should only ask for the "
                       "secrets its runtime.yaml declares. Usually the agent's repo is "
                       "missing a declaration, so add those names to its runtime.yaml. If "
                       "the agent should never use them, find out who asked."),
    "burst": ("Canopy watches for this because a normal turn reads an agent's secrets a "
              "few times, not dozens; a burst can mean a loop or someone harvesting them. "
              "Check which box and turn the reads came from."),
    "device-proof": ("Canopy refused this read because the agent only hands its secrets to "
                     "approved devices, and the caller could not prove it was one. If that "
                     "was your own box, approve its device key; if not, someone holds a "
                     "token without the device."),
    "zk-wrap-missing": ("Canopy refused this read because the device has not been granted "
                        "this agent's vault key. Grant the device if it should run the "
                        "agent."),
}


def _notify(agent, alert) -> dict:
    """Email + Web Push to each recipient. Never raises."""
    from django.core.mail import EmailMultiAlternatives

    result: dict = {"emailed": [], "failed": [], "not_configured": False, "pushed": 0}
    try:
        link = _link(agent)
        label = dict(alert.KIND_CHOICES).get(alert.kind, alert.kind)
        subject = f"Canopy vault alert · {agent.name or agent.slug}: {label}"
        why = _WHY.get(alert.kind, "")
        text = (f"{alert.detail}\n\n" + (f"{why}\n\n" if why else "")
                + f"See {agent.slug}'s credential read history: {link}\n\n"
                f"At most one alert of this kind per agent per hour. You are getting this "
                f"because you own {agent.slug} on Canopy.\n")
        html = (f"<p>{escape(alert.detail)}</p>"
                + (f"<p>{escape(why)}</p>" if why else "")
                + f'<p><a href="{escape(link)}">See {escape(agent.slug)}&rsquo;s credential read '
                f"history</a></p><p style=\"color:#78716c;font-size:13px\">At most one alert of "
                f"this kind per agent per hour. You are getting this because you own "
                f"{escape(agent.slug)} on Canopy.</p>")
        rel = link.split("://", 1)[-1]
        rel = rel[rel.find("/"):] if "/" in rel else "/"
        for person in recipients(agent):
            if person.email:
                message = EmailMultiAlternatives(subject=subject, body=text, to=[person.email])
                message.attach_alternative(html, "text/html")
                try:
                    if message.send():
                        result["emailed"].append(person.email)
                    else:
                        result["not_configured"] = True
                except Exception:  # noqa: BLE001
                    logger.exception("vault alert email failed: to=%s", person.email)
                    result["failed"].append(person.email)
            try:
                from apps.push.services import send_to_user

                result["pushed"] += send_to_user(person, f"Vault alert · {agent.slug}",
                                                 alert.detail[:180], rel)
            except Exception:  # noqa: BLE001
                logger.exception("vault alert push failed: user=%s", person.pk)
    except Exception:  # noqa: BLE001
        logger.exception("vault alert notify failed for %s", agent.slug)
        result["error"] = True
    return result


def _record_event(agent, alert) -> None:
    try:
        from apps.events import services as events

        events.record([{
            "source": "agents.credentials",
            "kind": "agent.credentials.alert",
            "level": "warn",
            "key": f"{agent.slug}:alert:{alert.kind}",
            "summary": f"vault alert for {agent.slug}: {alert.detail}"[:300],
            "payload": {"agent": agent.slug, "kind": alert.kind, "refs": alert.refs,
                        "alert": alert.pk, "notified": alert.notified},
        }], workspace=agent.workspace)
    except Exception:  # noqa: BLE001
        logger.exception("vault alert event failed for %s", agent.slug)


def alerts_for(agent, *, limit: int = 50) -> list[dict]:
    from .models import AgentCredentialAlert

    qs = (AgentCredentialAlert.objects.filter(agent=agent)
          .select_related("user", "runner").order_by("-created_at")[:limit])
    return [{
        "id": a.pk, "kind": a.kind, "detail": a.detail, "refs": list(a.refs or []),
        "user_email": a.user.email if a.user_id else "",
        "runner": str(a.runner_id) if a.runner_id else "",
        "runner_name": a.runner.name if a.runner_id else "",
        "turn_id": a.turn_id, "occurrences": a.occurrences,
        "notified": bool((a.notified or {}).get("emailed") or (a.notified or {}).get("pushed")),
        "created_at": a.created_at, "last_seen_at": a.last_seen_at,
    } for a in qs]
