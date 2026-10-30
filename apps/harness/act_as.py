"""An agent acting AS the person who started its turn, on canopy-web itself — the second
form of an act-as-you permission (the first is a connected site's host grant).

docs/architecture/access.md § Whose authority an agent acts with. The rules, each pinned
by `tests/test_act_as_you_on_canopy.py`:

* **Asked by the turn, for the turn's own person.** Only the login canopy-web issued for a
  turn (`TurnAgentToken`) may ask, and canopy-web works out WHO from that login alone
  (`turn_asker.check`): the turn is executing, unconfined, runs for this agent and was
  started by a verified, active human who is not an agent (or by a chain on their
  behalf). Nothing the agent sends names the person, so it can never ask to act as anyone
  else. A contact is never anyone's asker here, and has no passkey anyway.
* **Approved only with a fresh passkey touch.** `approve` runs only after a WebAuthn
  assertion made for THIS request (`api_act_as`: begin/finish, slot `act-as:<id>`), in the
  person's own browser; a session cookie, a step-up window or any token is not enough.
  The person picks the scope: `read` (GET routes and read-only tools) or `write`.
* **Never write for an unreviewed turn.** A turn in `auto` mode may ask only to read
  (`unreviewed_write_refusal`): an unreviewed turn holding the person's write authority is
  the one combination with no human anywhere in it. A reviewed (`manual`) turn is
  unaffected, since each action is already the person's call.
* **30 minutes from the touch, never extended** (`LIFETIME`; `expires_at` is written
  once). A new approval needs a new request and a new touch.
* **Dies with its turn, or its conversation.** Every use re-checks `held`: the turn it was
  asked from still executing — or, when it was asked in a conversation (a chat session, or
  a laptop's session-scoped login), that conversation still active with its newest turn
  still the person's. A chat reply carrying the approve link reaches the person only when
  the asking turn ends, and each later message is a new turn with a new login, so a
  request waits 30 minutes for the person whatever its turn does, and any later turn
  login of the same agent in that conversation for that person reads it, collects the
  token (once) and stops it (`agent_may`).
* **Returned once.** The agent collects the token on its first status read after the
  approval (`collect_token`); only its hash is kept, and later reads omit it.
* **One list of what it never reaches** (`EXCLUDED_PREFIXES`, `EXCLUDED_PATTERNS`,
  `EXCLUDED_TOOLS`): minting or rotating tokens, the person's memory switches and grants,
  passkeys, vaults, keys and secrets, act-as and host-grant approvals, agent-login
  acceptance, and the runner protocol. `route_refusal` is the one check both doors ask —
  REST (`tokens.middleware._authenticate_act_as_token`) and MCP (`mcp.act_as_scope`, and
  again inside the in-process request, `tokens.middleware._authenticate_mcp_call`).
* **Attributed "<agent> as <person>".** Every request it makes is a row in the MCP call log
  (`MCPAuditLog.acting_agent` / `act_as_grant`), its credential's label names the agent,
  and each transition is an event (`act_as.requested|approved|declined|stopped|expired`)
  on the agent's workspace — agent, person, turn and scope; the reason stays the person's.

A turn the token starts is not a verified asker's (`act_as` is not a verified assurance,
`caller_context._VERIFIED_USER`), so it never chains into another act-as permission.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import secrets
import uuid

from django.db import transaction
from django.utils import timezone

from apps.common.token_hash import hash_token
from apps.events import services as events
from apps.tokens import kinds

from . import turn_asker
from .models import ActAsGrant
from .routing import EXECUTING
from .turn_mode import AUTO

logger = logging.getLogger(__name__)

PREFIX = "cact_"
#: How long an approval lasts, from the passkey touch. Never extended.
LIFETIME = dt.timedelta(minutes=30)
#: How long a request waits for the person before it lapses. Long enough for a chat reply
#: carrying the link to reach them after the asking turn ends, and for the push.
REQUEST_TTL = dt.timedelta(minutes=30)
REASON_MAX = 500

_RUNNER = "the runner protocol and its enrolment"

# --- what an act-as token never reaches -------------------------------------------------
# ONE list, for both doors. Paths are FLAT (`/api/…`, the tenant prefix `/api/w/<ws>`
# stripped first); a route MCP does not serve as a tool is refused before these are asked.

#: Path prefixes: every route under them, every method, every scope.
EXCLUDED_PREFIXES: dict[str, str] = {
    "/api/tokens/": "minting, listing or revoking the person's tokens and connected apps",
    "/api/passkeys/": "the person's passkeys and passkey step-up",
    "/api/vault/": "the person's vault key",
    "/api/vaults/": "workspace vaults: their values, members and keys",
    "/api/me/act-as-requests/": "act-as-you permissions themselves",
    "/api/me/host-grants/": "act-as-you permissions on connected sites (host grants)",
    "/api/embed/": "a connected site's panel, where host grants are confirmed",
    "/api/me/agent-login-requests/": "accepting an agent's use of the person's account",
    "/api/me/delegated-credentials/": "credentials the person delegated to agents",
    "/api/people/me/": "the person's own memory switches, collections and grants",
    "/api/runner-sessions/": _RUNNER,
    "/api/runner-device-keys/": _RUNNER,
    "/api/runner-device-key-requests/": _RUNNER,
    "/api/harness/runner-pairings/": _RUNNER,
    "/api/harness/runner-invitations/": _RUNNER,
    "/api/harness/runner-credential-sets/": "runner credentials",
    "/api/workspaces/invites/": "accepting an invitation into a workspace",
}

#: Path patterns (over the flat path) for the families that sit inside other prefixes.
EXCLUDED_PATTERNS: dict[str, str] = {
    r"^/api/agents/[^/]+/(canopy-token|canopy-login|pat-rotation)(/|$)":
        "minting or rotating an agent's canopy token",
    r"^/api/agents/[^/]+/(vault|test-vault-key|credentials|credential-source|device-binding|google)(/|$)":
        "an agent's vault, credentials and keys",
    r"^/api/agents/[^/]+/secret-requests/[^/]+/value$": "filling an agent's secret",
    r"^/api/canopy-sessions/(my-keys|my-vault-key)$": "the person's conversation keys",
    r"^/api/canopy-sessions/[^/]+/(key-copies|secrets|sealed-attachments)(/|$)":
        "a conversation's key copies and secrets",
    r"^/api/canopy-sessions/attachments/[^/]+/sealed$": "sealed attachments",
    r"^/api/harness/runners/[^/]+/(vault|vault-keeper|mint|credential|credentials|logins|google-mint"
    r"|delegated-checks|turns/[^/]+/agent-token)(/|$)": "the runner protocol and runner credentials",
    r"^/api/(sessions|sessions/arcs|storyboards|walkthroughs)/[^/]+/rotate-token$": "rotating a share token",
    r"^/api/storyboards/[^/]+/share$": "minting a share token",
    r"^/api/slack-config/[^/]+/config-token$": "a Slack configuration token",
    r"^/api/workspaces/[^/]+/vaults(/|$)": "workspace vaults: their values, members and keys",
    r"^/api/workspaces/[^/]+/connected-apps(/|$)": "connected sites and the keys they sign visitors with",
}
_PATTERNS = tuple((re.compile(p), why) for p, why in EXCLUDED_PATTERNS.items())

#: canopy's hand-written MCP tools (not routes) an act-as token never calls. Any other
#: hand-written tool it may call only if it only reads (`mcp.act_as_scope`): what the
#: permission covers is the routes MCP serves as tools.
EXCLUDED_TOOLS: dict[str, str] = {
    "site_tools": "a connected site is reached only under that site's own act-as-you permission",
    "site_call": "a connected site is reached only under that site's own act-as-you permission",
    "site_read": "a connected site is reached only under that site's own act-as-you permission",
}

_TENANT_PATH = re.compile(r"^/api/w/[^/]+(?P<rest>/.*)$")
_SAFE = frozenset({"GET", "HEAD", "OPTIONS"})


def flat_path(path: str) -> str:
    tenant = _TENANT_PATH.match(path or "")
    return "/api" + tenant.group("rest") if tenant else (path or "")


def excluded(path: str) -> str | None:
    """Why an act-as token never reaches `path`, whatever its scope, or None."""
    flat = flat_path(path)
    for prefix, why in EXCLUDED_PREFIXES.items():
        if flat.startswith(prefix) or flat == prefix.rstrip("/"):
            return why
    for pattern, why in _PATTERNS:
        if pattern.search(flat):
            return why
    return None


def route_refusal(method: str, path: str, scope: str, *, is_tool: bool) -> str | None:
    """Why a request under an act-as-you permission of `scope` may not make `method path`,
    or None when it may. `is_tool`: the route is one canopy's MCP server serves as a tool
    (`mcp.api_tools.is_tool_route`) — the permission covers those and nothing else."""
    method = (method or "").upper()
    if not is_tool:
        return ("an act-as-you permission reaches only the routes canopy's MCP server serves "
                "as tools, and this is not one")
    why = excluded(path)
    if why is not None:
        return f"an act-as-you permission never reaches this ({why})"
    if scope != ActAsGrant.WRITE and method not in _SAFE:
        return ("this act-as-you permission is read-only: the agent may read as the person but "
                "change nothing. Ask them for read + write, or do it as the agent's own login.")
    return None


# --- status ---------------------------------------------------------------------------------


def dead_reason(grant: ActAsGrant, *, now=None) -> str | None:
    """Why an APPROVED grant no longer signs in, or None while it does."""
    now = now or timezone.now()
    if grant.status != ActAsGrant.APPROVED:
        return f"it is {grant.status}"
    if grant.expires_at is None or grant.expires_at <= now:
        return "its 30 minutes are up"
    if not held(grant):
        return "the turn it was approved for has ended"
    if not grant.person.is_active:
        return "the person's account is not active"
    return None


def held(grant: ActAsGrant) -> bool:
    """What keeps a request or a permission alive besides its clock: the turn it was asked
    from still runs — or, when it was asked in a conversation (a chat session, or a
    laptop's session-scoped login), that conversation is still active and its newest turn
    is still this person's. A chat reply carrying the approve link reaches the person only
    when the asking turn ends, and every message after is a new turn, so the conversation —
    not the one turn — is what the request belongs to."""
    if grant.turn.status in EXECUTING:
        return True
    if not grant.login_session_id:
        return False
    if not turn_asker.session_active(grant.login_session_id):
        return False
    person = turn_asker.session_person(grant.login_session_id)
    return person is not None and person.pk == grant.person_id


def session_of(turn, login=None):
    """The conversation a turn (or the session-scoped login serving it) belongs to, or None."""
    if login is not None and login.session_id:
        return login.session_id
    if turn.chat_session_id:
        return turn.chat_session_id
    sid = (turn.origin_ref or {}).get("chat_session_id") if isinstance(turn.origin_ref, dict) else None
    try:
        return uuid.UUID(str(sid)) if sid else None
    except ValueError:
        return None


def agent_turn(user, credential: dict | None):
    """The OnBehalf of a turn login now (session-scoped logins count while their session
    is held), or None."""
    try:
        return turn_asker.check(user, credential, session_login_live=True)
    except turn_asker.Refused:
        return None


def agent_may(user, credential: dict | None, grant: ActAsGrant) -> bool:
    """May this agent request read, collect or stop `grant`? The login that asked, or ANY
    turn login of the same agent now working in the same conversation for the same person
    — a later chat message is a new turn with a new login, and it is the one that sees the
    approval (exactly once: `collect_token`)."""
    cred = credential or {}
    if kinds.kind_of(cred) is not kinds.KINDS[kinds.PAT] or cred.get("id") is None:
        return False
    if user.pk != grant.agent.user_id:
        return False
    if int(cred["id"]) == grant.login_token_id:
        return True
    if not grant.login_session_id:
        return False
    ob = agent_turn(user, cred)
    return bool(ob is not None and ob.agent.pk == grant.agent_id and ob.user.pk == grant.person_id
                and session_of(ob.turn, ob.login) == grant.login_session_id)


def status_of(grant: ActAsGrant, *, now=None) -> str:
    """The status as of now: a lapsed request or a dead approval reads `expired` before
    `settle` has written it."""
    now = now or timezone.now()
    if grant.status == ActAsGrant.PENDING and (grant.request_expires_at <= now or not held(grant)):
        return ActAsGrant.EXPIRED
    if grant.status == ActAsGrant.APPROVED and dead_reason(grant, now=now) is not None:
        return ActAsGrant.EXPIRED
    return grant.status


def settle(grant: ActAsGrant) -> ActAsGrant:
    """Write `expired` for a request or approval that has run out, recording it once."""
    if grant.status not in (ActAsGrant.PENDING, ActAsGrant.APPROVED):
        return grant
    if status_of(grant) != ActAsGrant.EXPIRED:
        return grant
    changed = ActAsGrant.objects.filter(pk=grant.pk, status=grant.status).update(status=ActAsGrant.EXPIRED)
    grant.status = ActAsGrant.EXPIRED
    if changed:
        _event(grant, "act_as.expired")
    return grant


def expire_due() -> int:
    """The off-request clock's sweep: settle every request or approval that ran out."""
    now = timezone.now()
    from django.db.models import Q  # noqa: PLC0415 — the tick's only import

    due = (ActAsGrant.objects.select_related("person", "agent", "turn")
           .filter(Q(status=ActAsGrant.PENDING, request_expires_at__lte=now)
                   | Q(status=ActAsGrant.PENDING) & ~Q(turn__status__in=EXECUTING)
                   | Q(status=ActAsGrant.APPROVED, expires_at__lte=now)
                   | Q(status=ActAsGrant.APPROVED) & ~Q(turn__status__in=EXECUTING)))
    n = 0
    for grant in due[:500]:
        if settle(grant).status == ActAsGrant.EXPIRED:
            n += 1
    return n


# --- the lifecycle ---------------------------------------------------------------------------


class Refused(Exception):
    """The request or decision may not happen; the message says why."""

    def __init__(self, message: str, *, status: int = 403):
        super().__init__(message)
        self.status = status


def unreviewed_write_refusal(turn, scope: str) -> str | None:
    """Why `turn` may not hold `scope` as the person, or None: an `auto` turn is read-only."""
    if scope in (ActAsGrant.WRITE, ActAsGrant.EITHER) and getattr(turn, "turn_mode", "") == AUTO:
        return ("this turn runs unreviewed (auto), so it may act as you to read only; ask for read, "
                "or have the person run it manual")
    return None


def ask(user, credential: dict | None, *, reason: str, scope: str) -> ActAsGrant:
    """The agent's turn login asks to act as the person who started its turn. Raises Refused."""
    from apps.passkeys.models import Passkey  # noqa: PLC0415 — passkeys sits beside harness; only an ask pays

    reason = " ".join((reason or "").split())
    if not reason:
        raise Refused("say why the agent needs to act as them (reason)", status=422)
    if len(reason) > REASON_MAX:
        raise Refused(f"the reason is at most {REASON_MAX} characters", status=422)
    if scope not in ActAsGrant.REQUESTABLE:
        raise Refused(f"scope is one of {', '.join(ActAsGrant.REQUESTABLE)}", status=422)
    try:
        ob = turn_asker.check(user, credential, session_login_live=True)
    except turn_asker.Refused as exc:
        raise Refused(f"no one to act as: {exc}") from exc
    if (why := unreviewed_write_refusal(ob.turn, scope)):
        raise Refused(why)
    if not Passkey.objects.filter(user=ob.user).exists():
        raise Refused(f"{ob.user.email} has no passkey, so they cannot approve an act-as-you "
                      "permission; they register one in canopy Settings → Passkeys")
    now = timezone.now()
    session_id = session_of(ob.turn, ob.login)
    with transaction.atomic():
        same = (dict(agent=ob.agent, person=ob.user, login_session_id=session_id) if session_id
                else dict(turn=ob.turn))
        waiting = [g for g in ActAsGrant.objects.select_for_update(of=("self",))
                   .select_related("turn", "person", "agent")
                   .filter(status=ActAsGrant.PENDING, **same)
                   if status_of(g, now=now) == ActAsGrant.PENDING]
        if waiting:
            raise Refused(f"a request is already waiting for {ob.user.email} ({waiting[0].pk}); "
                          "wait for it, or stop it first", status=409)
        grant = ActAsGrant.objects.create(
            person=ob.user, agent=ob.agent, turn=ob.turn, login_token_id=int(credential["id"]),
            login_session_id=session_id,
            login_runner_id=ob.login.runner_id if ob.login is not None and ob.login.session_id else None,
            reason=reason, requested_scope=scope, request_expires_at=now + REQUEST_TTL)
    pushed = _notify(grant)
    _event(grant, "act_as.requested", requested_scope=scope, pushed=pushed)
    return grant


def approve(grant: ActAsGrant, person, scope: str) -> ActAsGrant:
    """The person approved, AFTER the passkey assertion made for this request (the caller
    verifies it). Starts the 30 minutes. Raises Refused."""
    if scope not in ActAsGrant.GRANTABLE:
        raise Refused("grant read or write", status=422)
    if scope == ActAsGrant.WRITE and grant.requested_scope == ActAsGrant.READ:
        raise Refused("the agent asked only to read; it cannot be given write", status=422)
    now = timezone.now()
    with transaction.atomic():
        row = ActAsGrant.objects.select_for_update(of=("self",)).select_related("turn", "person", "agent").get(pk=grant.pk)
        if row.person_id != person.pk:
            raise Refused("this request is not yours to approve", status=404)
        if status_of(row, now=now) != ActAsGrant.PENDING:
            raise Refused(f"this request is {status_of(row, now=now)}; the agent has to ask again",
                          status=409)
        if (why := unreviewed_write_refusal(row.turn, scope)):
            raise Refused(why, status=422)
        row.status, row.scope = ActAsGrant.APPROVED, scope
        row.approved_at = row.decided_at = now
        row.expires_at = now + LIFETIME
        row.save(update_fields=["status", "scope", "approved_at", "decided_at", "expires_at"])
    _event(row, "act_as.approved", scope=scope)
    return row


def decline(grant: ActAsGrant, person) -> ActAsGrant:
    if grant.person_id != person.pk:
        raise Refused("this request is not yours to decline", status=404)
    now = timezone.now()
    changed = ActAsGrant.objects.filter(pk=grant.pk, status=ActAsGrant.PENDING).update(
        status=ActAsGrant.DECLINED, decided_at=now)
    grant.refresh_from_db()
    if changed:
        _event(grant, "act_as.declined")
    return grant


def stop(grant: ActAsGrant, *, by: str) -> ActAsGrant:
    """End it now — a waiting request or a live approval. `by`: "agent" | "person"."""
    now = timezone.now()
    changed = (ActAsGrant.objects.filter(pk=grant.pk, status__in=(ActAsGrant.PENDING, ActAsGrant.APPROVED))
               .update(status=ActAsGrant.STOPPED, stopped_at=now, stopped_by=by))
    grant.refresh_from_db()
    if changed:
        _event(grant, "act_as.stopped", stopped_by=by)
    return grant


def collect_token(grant: ActAsGrant) -> str | None:
    """The token, the FIRST time the agent reads a live approval; None ever after. Only
    its hash is stored."""
    with transaction.atomic():
        row = (ActAsGrant.objects.select_for_update(of=("self",)).select_related("turn", "person")
               .get(pk=grant.pk))
        if row.token_hash or dead_reason(row) is not None:
            return None
        raw = PREFIX + secrets.token_urlsafe(32)
        row.token_hash, row.token_issued_at = hash_token(raw), timezone.now()
        row.save(update_fields=["token_hash", "token_issued_at"])
    grant.token_hash, grant.token_issued_at = row.token_hash, row.token_issued_at
    return raw


# --- the token ---------------------------------------------------------------------------------


def resolve(raw: str) -> tuple[ActAsGrant | None, str]:
    """The live grant behind a raw token, or (None, why not). Re-checked on every request."""
    if not raw or not raw.startswith(PREFIX):
        return None, "not an act-as-you token"
    grant = (ActAsGrant.objects.select_related("person", "agent", "turn")
             .filter(token_hash=hash_token(raw)).first())
    if grant is None:
        return None, "no such act-as-you permission"
    return _live(grant)


def live_by_id(grant_id) -> tuple[ActAsGrant | None, str]:
    """The same check, for the grant an in-process MCP request names (its token was
    resolved by the MCP server a moment earlier)."""
    grant = (ActAsGrant.objects.select_related("person", "agent", "turn")
             .filter(pk=grant_id, token_hash__isnull=False).first()) if grant_id else None
    if grant is None:
        return None, "no such act-as-you permission"
    return _live(grant)


def _live(grant: ActAsGrant) -> tuple[ActAsGrant | None, str]:
    why = dead_reason(grant)
    if why is not None:
        settle(grant)
        return None, why
    return grant, ""


def credential(grant: ActAsGrant) -> dict:
    """What a request made with the token records as its credential — every audit trail
    that names a credential (settings changes, turn provenance) then reads "<agent> as
    <person>"."""
    return {"type": kinds.ACT_AS, "id": str(grant.pk), "label": attribution(grant),
            "turn_id": str(grant.turn_id), "act_as_grant": str(grant.pk),
            "acting_agent": grant.agent.slug, "act_as_scope": grant.scope}


def attribution(grant: ActAsGrant) -> str:
    return f"{grant.agent.slug} as {grant.person.email}"


def touch(grant: ActAsGrant) -> None:
    now = timezone.now()
    if grant.last_used_at is None or now - grant.last_used_at > dt.timedelta(seconds=60):
        ActAsGrant.objects.filter(pk=grant.pk).update(last_used_at=now)


# --- the person hears about it ------------------------------------------------------------------


def approve_path(grant: ActAsGrant) -> str:
    return f"/act-as/{grant.pk}"


def _notify(grant: ActAsGrant) -> int:
    """A push to the person's devices; the request is on their supervisor feed and in
    Settings either way. Returns how many devices the push service accepted it for (0: no
    subscribed device, push not configured, or it failed), recorded on the grant and in the
    `act_as.requested` event so "did the push fire?" has an answer. Never raises."""
    sent = 0
    try:
        from apps.push.services import send_to_user  # noqa: PLC0415 — as sign_in_requests

        name = grant.agent.name or grant.agent.slug
        asked = {"read": "read as you", "write": "act as you (read + write)",
                 "either": "act as you"}[grant.requested_scope]
        sent = send_to_user(grant.person, title=f"{name} asks to {asked} on canopy",
                            body=f"{grant.reason[:160]} — tap to decide with your passkey.",
                            url=approve_path(grant), ttl=int(REQUEST_TTL.total_seconds()),
                            urgency="high") or 0
    except Exception:  # noqa: BLE001 — the row is the record; a push is best-effort
        logger.exception("act-as: could not push request %s", grant.pk)
        sent = -1
    ActAsGrant.objects.filter(pk=grant.pk).update(push_sent=sent)
    grant.push_sent = sent
    logger.info("act-as: request %s pushed to %d device(s)", grant.pk, sent)
    return sent


def _event(grant: ActAsGrant, kind: str, **extra) -> None:
    """One event per transition on the agent's workspace, for its admins: agent, person,
    turn and scope — never the reason, which is the person's. Never raises."""
    verb = {"act_as.requested": "asked to act as", "act_as.approved": "may act as",
            "act_as.declined": "was declined acting as", "act_as.stopped": "stopped acting as",
            "act_as.expired": "no longer acts as"}[kind]
    scope = extra.get("scope") or grant.scope or extra.get("requested_scope") or ""
    try:
        events.record([{
            "source": "harness.act_as",
            "kind": kind,
            "level": "info",
            "key": f"{grant.pk}:{kind}",
            "summary": f"{grant.agent.slug} {verb} {grant.person.email} on canopy"
                       + (f" ({scope})" if scope else ""),
            "payload": {"agent": grant.agent.slug, "person": grant.person.email,
                        "turn": str(grant.turn_id), "grant": str(grant.pk),
                        "scope": grant.scope, "requested_scope": grant.requested_scope, **extra},
        }], workspace=grant.agent.workspace)
    except Exception:  # noqa: BLE001 — the record must not undo the transition
        logger.exception("act-as: could not record %s for %s", kind, grant.pk)
