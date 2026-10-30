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
  A box that names itself AND sends `X-Canopy-Read-Purpose: runner-refresh` is
  its own scheduled credential refresh, also expected without a turn. The owner's
  own token proved by their own laptop's device key is the owner opening a
  session there — an `admin` read, not a box (2026-10-18).
* `undeclared-ref` — a read that asks for, or is handed, a ref the agent does not
  declare: not among its declared secrets on canopy-web (`runtime_secrets`, written by
  `canopy agent vault` — the vault is the one declaration since 2026-10-22)
  nor supplied by a layered workspace vault.
* `burst` — more than CANOPY_VAULT_READ_BURST_LIMIT resolves (default 50) of one
  agent inside a 5-minute window. Counted per resolve request, not per ref: one
  unscoped resolve of ACE hands out 45 refs and is one read.
* `device-proof` — a resolve refused for a bad or missing device proof. NOT a
  good proof from an approved box that does not run the agent: that is routing,
  recorded as an `agent.credentials.refused` event with no alert
  (`unrouted_box_refused`, canopy-web#1563).

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
import uuid
from html import escape

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.events import services as events
from apps.push.services import send_to_user
from apps.workspaces import links as wslinks

from .. import services
from ..models import Agent, AgentAdmin, AgentCredentialAlert

logger = logging.getLogger(__name__)

DEBOUNCE = dt.timedelta(hours=1)
#: `X-Canopy-Read-Purpose` a box sends when it refreshes an agent's credentials
#: outside a turn (canopy `agent prepare --runner`).
REFRESH_PURPOSE = "runner-refresh"
#: `X-Canopy-Read-Purpose: check: <why> (by <user@host>)` — a verification run
#: (canopy `agent prepare --check`). Alerted on, labelled; never exempt.
CHECK_PREFIX = "check:"
BURST_WINDOW_S = 300


def _burst_limit() -> int:
    return int(getattr(settings, "CANOPY_VAULT_READ_BURST_LIMIT", 50) or 50)


def after_read(agent, request, *, via: str, turn_id: str, asked: set[str] | None,
               handed: list[str], runner=None) -> list:
    """Check one SUCCESSFUL resolve. `via` is resolve's principal (`runner` or
    `admin`), `asked` the `?refs=` scope (None when unscoped), `handed` the
    credential refs whose values went out. Returns the alerts raised (new rows
    only)."""
    raised = []
    try:
        user = request.user
        # A box's own credential refresh (the runner-update timer, bootstrap) runs
        # outside any turn by design and says so. It cannot hide a misuse the turn
        # id would have caught: a turn id is not checked against a live turn either,
        # so this alert only ever catches an honest caller nobody expected
        # (canopy-web#1563). The read is still logged; the other checks still run.
        purpose = request.META.get("HTTP_X_CANOPY_READ_PURPOSE", "")
        refresh = runner is not None and purpose == REFRESH_PURPOSE
        if not turn_id and via == "runner" and not refresh:
            box = f" on {runner.name}" if runner is not None else ""
            # A person's or agent's verification run (`canopy agent prepare --check`)
            # says so. It is NOT exempt — anyone holding the token can send the
            # header — it is only worded as the check it claims to be, under its own
            # kind so it never uses up the hour's alert for an unlabelled read.
            check = purpose[len(CHECK_PREFIX):].strip() if purpose.startswith(CHECK_PREFIX) else ""
            if check:
                raised.append(raise_alert(
                    agent, "no-turn-check", user=user, runner=runner, refs=sorted(handed),
                    request=request,
                    detail=f"a box{box} ({_who(user)}) read {agent.slug}'s secrets outside a turn, "
                           f"labelled as a verification check: {' '.join(check.split())[:200]}"))
            else:
                raised.append(raise_alert(
                    agent, "no-turn", user=user, runner=runner, request=request,
                    detail=f"a box{box} ({_who(user)}) read {agent.slug}'s secrets outside a turn "
                           "(no X-Canopy-Turn-Id)",
                    refs=sorted(handed)))
        # A name a workspace vault layered under the agent holds is declared by that
        # vault, not missing from the agent's own declaration (Jonathan, 2026-10-22: the gog OAuth
        # clients are the same for every dimagi agent and live in the workspace vault).
        from . import workspace_vaults  # noqa: PLC0415 — a cycle: zk imports this module, workspace_vaults imports zk
        declared = services.declared_credential_names(agent) | workspace_vaults.layered_names(agent)
        undeclared = sorted(((asked or set()) | set(handed)) - declared - _EXTRAS)
        if undeclared:
            raised.append(raise_alert(
                agent, "undeclared-ref", user=user, runner=runner, turn_id=turn_id,
                request=request,
                detail=f"{_who(user)} asked for refs {agent.slug} does not declare: "
                       + ", ".join(undeclared)[:300],
                refs=undeclared))
        count = _count_read(agent)
        if count > _burst_limit():
            raised.append(raise_alert(
                agent, "burst", user=user, runner=runner, turn_id=turn_id, request=request,
                detail=f"{count} reads of {agent.slug}'s secrets in under 5 minutes "
                       f"(limit {_burst_limit()})"))
    except Exception:  # noqa: BLE001 - an alert hiccup must not deny a runner its secrets
        logger.exception("vault read-anomaly check failed for %s", agent.slug)
    return [a for a in raised if a is not None]


def device_proof_refused(agent, request, *, reason: str, runner=None):
    """A resolve answered 401 `device_proof_required`.

    The detail says WHO sent it in words an owner can act on. A runner token signs in
    as the runner's owner, so "resolve by <owner>" read as something the owner had done
    and "resolve" as "resolved" (Jonathan, 2026-10-30: "why is this saying resolved by
    jjackson@dimagi.com, I don't think I took any actions")."""
    try:
        who = _who(request.user)
        if getattr(request, "runner_token", None) is not None and runner is not None:
            detail = (f"The runner {runner.name} asked for {agent.slug}’s secrets "
                      f"without proving which machine it is, and was refused ({reason}). "
                      f"Runners sign in to Canopy as their owner, which is why {who} is "
                      f"named; it was the runner’s software, not something {who} did.")
        else:
            detail = (f"A request using {who}’s login"
                      + (f" on {runner.name}" if runner is not None else "")
                      + f" asked for {agent.slug}’s secrets and was refused: {reason}.")
        return raise_alert(
            agent, "device-proof", user=request.user, runner=runner, request=request,
            turn_id=request.META.get("HTTP_X_CANOPY_TURN_ID", "")[:120],
            detail=detail[:500])
    except Exception:  # noqa: BLE001
        logger.exception("vault device-proof alert failed for %s", agent.slug)
        return None


def unrouted_box_refused(agent, request, *, runner) -> None:
    """A resolve refused because the proving box, approved and active, does not
    run this agent. Logged and recorded as an `agent.credentials.refused` event,
    never an owner alert: nothing was handed out, the box is a known one, and the
    old alert's advice ("approve its device key") was wrong for it — routing, not
    the key, is what said no (fizzy, 2026-10-17, canopy-web#1563). A box that
    keeps asking shows on the event's count, and its owner can see it there."""
    try:
        logger.info("resolve of %s refused: %s does not run it", agent.slug, runner.name)

        events.record([{
            "source": "agents.credentials",
            "kind": "agent.credentials.refused",
            "level": "info",
            "key": f"{agent.slug}:refused:{runner.pk}",
            "summary": (f"{runner.name} asked for {agent.slug}'s secrets but does not run "
                        f"{agent.slug}; refused, nothing handed out")[:300],
            "payload": {"agent": agent.slug, "runner": str(runner.pk),
                        "runner_name": runner.name, "user": _who(request.user)},
        }], workspace=agent.workspace)
    except Exception:  # noqa: BLE001 - must not turn a 401 into a 500
        logger.exception("unrouted-box event failed for %s", agent.slug)


#: Names an older canopy CLI still sends that are fields, not refs an agent declares, so
#: naming them is never "undeclared". `github_token` and `op_sa_token` were fields once;
#: neither is ever returned now.
_EXTRAS = {"op_sa_token", "github_token"}


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


#: Repeats kept per alert row: enough to see a pattern inside the hour, bounded.
MAX_REPEATS = 20


def raise_alert(agent, kind: str, *, detail: str, refs=(), user=None, runner=None,
                turn_id: str = "", request=None):
    """Record (and, unless one of this kind is open this hour, notify) an alert.
    Returns the new row, or None when it only counted against an open one.

    With `request`, who asked is kept (`request_context`) — on the row the first time,
    and appended to `repeats` for each repeat the debounce swallows."""

    now = timezone.now()
    context = request_context(request, turn_id) if request is not None else {}
    with transaction.atomic():
        # Serialise per agent so two concurrent reads cannot both open an alert.
        Agent.objects.select_for_update().filter(pk=agent.pk).first()
        open_ = (AgentCredentialAlert.objects
                 .filter(agent=agent, kind=kind, created_at__gte=now - DEBOUNCE)
                 .order_by("-created_at").first())
        if open_ is not None:
            repeats = list(open_.repeats or [])
            if len(repeats) < MAX_REPEATS:
                repeats.append({"at": now.isoformat(), "detail": detail[:300], **context})
            AgentCredentialAlert.objects.filter(pk=open_.pk).update(
                occurrences=F("occurrences") + 1, last_seen_at=now, repeats=repeats)
            return None
        alert = AgentCredentialAlert.objects.create(
            agent=agent, kind=kind, detail=detail[:500], refs=list(refs)[:100],
            user=user if getattr(user, "is_authenticated", False) else None,
            runner=runner, turn_id=(turn_id or "")[:120], context=context,
        )
    alert.notified = _notify(agent, alert)
    AgentCredentialAlert.objects.filter(pk=alert.pk).update(notified=alert.notified)
    _record_event(agent, alert)
    return alert


def _who(user) -> str:
    return getattr(user, "email", "") or "someone"


#: Request headers worth keeping on an alert — identity, never a value. The canopy CLI
#: sends the first four on every call (`orchestrator/provenance.py`); `proof_missing` is
#: its reason for sending no device proof (`device_key.proof_headers`).
_CONTEXT_HEADERS = (
    ("client", "HTTP_X_CANOPY_CLIENT"),
    ("user_agent", "HTTP_USER_AGENT"),
    ("parent_session", "HTTP_X_CANOPY_PARENT_SESSION"),
    ("claude_session", "HTTP_X_CANOPY_CLAUDE_SESSION"),
    ("proof_missing", "HTTP_X_CANOPY_PROOF_MISSING"),
    ("purpose", "HTTP_X_CANOPY_READ_PURPOSE"),
)


#: `context["via"]` when the request came on a runner's own token, which signs in as
#: the runner's owner: the alert is the box's doing, not the person's.
RUNNER_TOKEN = "runner-token"


def request_context(request, turn_id: str = "") -> dict:
    """Who asked, as far as the request says and canopy-web can resolve: the headers
    above plus what the named turn IS (`turn_summary`). Best-effort, never raises."""
    out: dict = {}
    try:
        for key, header in _CONTEXT_HEADERS:
            value = " ".join(str(request.META.get(header, "")).split())[:200]
            if value:
                out[key] = value
        if getattr(request, "runner_token", None) is not None:
            out["via"] = RUNNER_TOKEN
        turn = turn_summary(turn_id or request.META.get("HTTP_X_CANOPY_TURN_ID", ""))
        if turn:
            out["turn"] = turn
    except Exception:  # noqa: BLE001 — context is an addition to the alert, never its failure
        logger.exception("vault alert context failed")
    return out


def turn_summary(turn_id: str) -> dict:
    """What a turn id names, for the agent's admins: its agent (or project), origin,
    status, runner, chat session and who started it — never its prompt or transcript.
    {"id", "found": False} when the id names no turn (a mistyped or forged header is
    itself worth seeing)."""
    raw = (turn_id or "").strip()
    if not raw:
        return {}
    from apps.harness.models import Turn  # noqa: PLC0415 — agents→harness only here

    try:
        tid = uuid.UUID(raw)
    except ValueError:
        return {"id": raw[:120], "found": False}
    t = (Turn.objects.select_related("agent", "claimed_by", "initiator_user",
                                     "chat_session__agent", "chat_session__workspace")
         .filter(pk=tid).first())
    if t is None:
        return {"id": raw, "found": False}
    # A chat turn targets its conversation, not the agent (turn_targets_agent_xor_
    # project_xor_session), so the agent is the conversation's — else the email said
    # "a session's canopy_web_chat turn" (2026-10-30).
    agent = t.agent if t.agent_id else (t.chat_session.agent if t.chat_session_id else None)
    out = {"id": raw, "found": True, "origin": t.origin, "status": t.status,
           "agent": agent.slug if agent is not None else "",
           "project": getattr(t, "project", "") or "",
           "runner": t.claimed_by.name if t.claimed_by_id else "",
           "started_by": t.initiator_user.email if t.initiator_user_id else "",
           "created_at": t.created_at.isoformat() if t.created_at else ""}
    if t.chat_session_id:
        out["chat_session"] = str(t.chat_session_id)
        out["chat_url"] = wslinks.scoped_url(t.chat_session.workspace,
                                             f"/sessions/{t.chat_session_id}")
    return out


#: A turn's origin in an owner's words, not the enum.
_ORIGIN_WORDS = {"canopy_web_chat": "Canopy chat", "slack": "Slack", "email": "email",
                 "canopy_scheduler": "scheduled", "api": "API"}


def context_lines(context: dict) -> list[str]:
    """`context` as short plain lines for the email and the alert list."""
    lines = []
    turn = context.get("turn") or {}
    if turn.get("found"):
        origin = turn.get("origin") or ""
        kind = " ".join(x for x in (_ORIGIN_WORDS.get(origin, origin), "turn") if x)
        what = turn.get("agent") or turn.get("project")
        bits = [f"during {what}’s {kind}" if what else f"during a {kind}"]
        if turn.get("started_by"):
            bits.append(f"started by {turn['started_by']}")
        if turn.get("runner"):
            bits.append(f"on {turn['runner']}")
        lines.append("When: " + ", ".join(bits) + f" (turn {turn['id']})")
        if turn.get("chat_url"):
            lines.append(f"The conversation: {turn['chat_url']}")
        elif turn.get("chat_session"):
            lines.append(f"Chat session: {turn['chat_session']}")
    elif turn.get("id"):
        lines.append(f"Turn {turn['id']}: no such turn on canopy-web")
    if context.get("proof_missing"):
        lines.append(f"Why the caller sent no proof: {context['proof_missing']}")
    client = context.get("user_agent") or context.get("client")
    if client:
        lines.append(f"Client: {client}")
    if context.get("claude_session"):
        lines.append(f"Claude session: {context['claude_session']}")
    return lines


def recipients(agent) -> list:
    """The agent's owner; with none, its explicit admins (still members)."""
    if agent.owner_id and agent.owner.is_active:
        return [agent.owner]

    return [g.user for g in AgentAdmin.objects.filter(agent=agent).select_related("user")
            if g.user.is_active and agent.is_admin(g.user)]


#: Where each kind's action is: the Security tab's section that holds it, not the top of
#: the old one-page Settings. A refused device proof or a missing key wrap is fixed among
#: the agent's machines (approve, grant, revoke); the rest are read in the alert list,
#: beside the read history.
_ACT_AT = {"device-proof": ("cred-device-keys", "Review {slug}\u2019s approved machines"),
           "zk-wrap-missing": ("cred-device-keys", "Review {slug}\u2019s approved machines")}
_DEFAULT_ACT = ("cred-alerts", "See {slug}\u2019s vault alerts and credential read history")


def _link(agent, kind: str = "") -> str:

    anchor, _ = _ACT_AT.get(kind, _DEFAULT_ACT)
    return wslinks.scoped_url(agent.workspace, f"/agents/{agent.slug}/settings/security#{anchor}")


def _link_label(agent, kind: str) -> str:
    return _ACT_AT.get(kind, _DEFAULT_ACT)[1].format(slug=agent.slug)


#: What each kind watches for and what the owner should do, in plain words. The
#: detail line says what was seen; without this an owner cannot tell a misused
#: token from a config gap (canopy-web#1563).
_WHY = {
    "no-turn": ("Canopy watches for this because a runner box should only read an agent's "
                "secrets while it is running one of the agent's turns; a box reading them "
                "at any other time is what a stolen or misused runner token looks like. "
                "If you were not working on that box, pause it and revoke its token."),
    "no-turn-check": ("The box said this was a verification check, and named who ran it — "
                      "expected if you or one of your agents was checking that the agent's "
                      "secrets load on that box. The label is the caller's own word, not "
                      "proof: if nobody was checking, treat it as a read outside a turn — "
                      "pause the box and revoke its token."),
    "undeclared-ref": ("Canopy watches for this because an agent should only ask for the "
                       "secrets declared in its vault on Canopy. If the agent needs it, store it in the agent's "
                       "vault (`canopy agent vault`), which declares it. If the agent "
                       "should never use it, find out who asked."),
    "burst": ("Canopy watches for this because a normal turn reads an agent's secrets a "
              "few times, not dozens; a burst can mean a loop or someone harvesting them. "
              "Check which box and turn the reads came from."),
    "device-proof": ("Canopy refused this read because the agent only hands its secrets to "
                     "approved devices, and the caller could not prove it was one. Nothing "
                     "was handed out. \"No device proof\" means the request carried none at "
                     "all: your token used away from a machine's device key — a script, a test "
                     "run, a fresh install — not a box waiting for approval. A proof from a key "
                     "nobody approved means a machine to approve, if it is yours. If you do "
                     "not recognise either, revoke the token. (A known box that simply does "
                     "not run the agent is refused without this alert.)"),
    # The same refusal when the caller was a runner on its own token: the box's
    # software, not the person it signs in as (trellis on cloud-ec2-live, 2026-10-30).
    "device-proof:runner": ("What this means: this agent only hands its secrets to "
                            "machines that prove which machine they are. A runner asked without that "
                            "proof, so Canopy refused it. Nothing was handed out, and you did "
                            "not do anything: every request a runner makes carries its "
                            "owner’s name. Look into it only if you do not recognise "
                            "the turn above or the runner is not yours. Every alert like "
                            "this, with each repeat and the turn it came from, is at the "
                            "second link below."),
    "zk-wrap-missing": ("Nothing is wrong or unsafe. Approving a machine lets it identify itself; "
                        "it does not hand it the agent's secrets. An admin of the agent does "
                        "that separately, once per machine. Open the link, find the machine "
                        "under the agent's approved machines, choose Grant, and confirm with "
                        "your passkey. Until then, every turn that machine runs for this "
                        "agent fails on its first secret read. The person named above "
                        "has no way to fix this themselves."),
}


def _why(alert) -> str:
    """The explanation for `alert`: its kind's, or the runner-token variant of it."""
    if (alert.context or {}).get("via") == RUNNER_TOKEN:
        runner_why = _WHY.get(f"{alert.kind}:runner")
        if runner_why:
            return runner_why
    return _WHY.get(alert.kind, "")


#: Subject wording for kinds whose model label reads as internals.
_SUBJECT = {"zk-wrap-missing": "a newly approved machine needs access granted",
            "device-proof": "a request for its secrets was refused (no machine proof)"}


def _notify(agent, alert) -> dict:
    """Email + Web Push to each recipient. Never raises."""
    from django.core.mail import EmailMultiAlternatives

    result: dict = {"emailed": [], "failed": [], "not_configured": False, "pushed": 0}
    try:
        link = _link(agent, alert.kind)
        act = _link_label(agent, alert.kind)
        label = dict(alert.KIND_CHOICES).get(alert.kind, alert.kind)
        label = _SUBJECT.get(alert.kind, label)
        subject = f"Canopy vault alert · {agent.name or agent.slug}: {label}"
        why = _why(alert)
        who = context_lines(alert.context or {})
        log = _link(agent)
        log_label = _link_label(agent, "")
        text = (f"{alert.detail}\n\n" + ("".join(f"{line}\n" for line in who) + "\n" if who else "")
                + (f"{why}\n\n" if why else "")
                + f"{act}: {link}\n"
                + (f"{log_label}: {log}\n" if log != link else "") + "\n"
                f"At most one alert of this kind per agent per hour. You are getting this "
                f"because you own {agent.slug} on Canopy.\n")
        html = (f"<p>{escape(alert.detail)}</p>"
                + ("<p>" + "<br>".join(escape(line) for line in who) + "</p>" if who else "")
                + (f"<p>{escape(why)}</p>" if why else "")
                + f'<p><a href="{escape(link)}">{escape(act)}</a>'
                + (f'<br><a href="{escape(log)}">{escape(log_label)}</a>' if log != link else "")
                + "</p>"
                f"<p style=\"color:#78716c;font-size:13px\">At most one alert of "
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
                    logger.exception("vault alert email failed: user=%s", person.pk)
                    result["failed"].append(person.email)
            try:

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

    qs = (AgentCredentialAlert.objects.filter(agent=agent)
          .select_related("user", "runner").order_by("-created_at")[:limit])
    return [{
        "id": a.pk, "kind": a.kind, "detail": a.detail, "refs": list(a.refs or []),
        "user_email": a.user.email if a.user_id else "",
        "runner": str(a.runner_id) if a.runner_id else "",
        "runner_name": a.runner.name if a.runner_id else "",
        "turn_id": a.turn_id, "occurrences": a.occurrences,
        "context": a.context or {}, "repeats": list(a.repeats or []),
        "notified": bool((a.notified or {}).get("emailed") or (a.notified or {}).get("pushed")),
        "created_at": a.created_at, "last_seen_at": a.last_seen_at,
    } for a in qs]
