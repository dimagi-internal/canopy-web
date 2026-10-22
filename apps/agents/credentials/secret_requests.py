"""An agent asks for a declared secret; a person answers with ciphertext.

Spec: docs/superpowers/specs/2026-10-19-give-an-agent-a-secret-design.md.

The agent's session calls `request_agent_secret` (a route, so an MCP tool too)
and the chat shows a seal card under that tool call. The card seals the value in
the person's browser to the agent's endorsed recipients (`zks1`, zk-v1 §6/§7)
and sends only that. Nothing here — model, route or tool result — ever carries a
value: a request has a name, a reason and a status.

Who may answer (`may_answer`), the "secret writer" gate:

* an agent admin — fill or replace;
* a person who can WRITE to the session the request was asked from, and is a
  member of the agent's workspace — fill a MISSING value only. Replacing a
  stored token with one the attacker controls is an integrity attack even with
  nothing read, so replacing stays admin-only;
* never an agent's own login, not even its own agent's: a prompt-injected agent
  must not be able to "answer" its request with a token it was handed.
"""
from __future__ import annotations

import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.canopy_sessions import access
from apps.canopy_sessions.models import Message
from apps.harness import provenance
from apps.workspaces import services as wsvc

from .. import services
from ..models import Agent, AgentCredential, AgentSecretRequest
from . import zk
from .zk import ZkError
from .zk_seal import SEALED_PREFIX

TTL = timedelta(hours=24)


class RequestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def is_agent_login(user) -> bool:

    return bool(getattr(user, "is_authenticated", False)) and Agent.objects.filter(user=user).exists()


def _declared(agent, name: str) -> str:

    name = (name or "").strip()
    declared = services.declared_credential_names(agent)
    if name not in declared:
        raise RequestError(422, "not_declared",
                           f"{name or '(empty)'} is not a secret {agent.slug} declares in runtime.yaml"
                           + (f" (declared: {', '.join(sorted(declared))})" if declared
                              else " — it declares none"))
    return name


def _session_of_caller(agent):
    """The chat this request was asked from — the parent session canopy's MCP
    headers name (`X-Canopy-Parent-Session`/`-Turn`) — but only one of THIS
    agent's own sessions: a header is the caller's claim, and the session's
    writers become eligible answerers."""

    fields, _ = provenance.parent_fields()
    session = fields.get("parent_session")
    if session is None or session.agent_id != agent.pk:
        return None
    return session


def live(req) -> bool:
    return req.status == req.OPEN and req.expires_at > timezone.now()


def has_value(agent, name: str) -> bool:

    return AgentCredential.objects.filter(agent=agent, name=name).exists()


def create(agent, *, name: str, reason: str, user):
    """Open (or return the open) request for `name` from this chat."""

    if not (agent.is_admin(user) or agent.is_own_login(user)):
        raise RequestError(403, "forbidden", f"only {agent.slug} itself or its admins may ask for its secrets")
    if not agent.zk_enabled:
        raise RequestError(409, "zk_not_enabled",
                           f"{agent.slug}'s secrets are not zero-knowledge: store them from its Credentials page")
    name = _declared(agent, name)
    session = _session_of_caller(agent)
    now = timezone.now()
    existing = (AgentSecretRequest.objects
                .filter(agent=agent, name=name, session=session, status=AgentSecretRequest.OPEN,
                        expires_at__gt=now).first())
    if existing is not None:
        return existing
    return AgentSecretRequest.objects.create(
        agent=agent, name=name, reason=(reason or "").strip()[:300], session=session,
        requested_by=user if getattr(user, "is_authenticated", False) else None, expires_at=now + TTL)


def may_read(req, user) -> bool:

    return req.agent.is_admin(user) or (req.session is not None and access.can_read(user, req.session))


def may_answer(req, user) -> bool:
    """Admin, or a writer of the request's session who is a member of the
    agent's workspace — never an agent's own login. Replacing is `may_replace`."""

    if is_agent_login(user):
        return False
    if req.agent.is_admin(user):
        return True
    return (req.session is not None and access.can_write(user, req.session)
            and wsvc.is_member(user, req.agent.workspace.slug))


def fulfil(req, *, kv: int, value_zk: str, user) -> None:
    """Store a `zks1` value for the request's name, atomically with closing it."""

    if not isinstance(value_zk, str) or not value_zk.startswith(SEALED_PREFIX):
        raise RequestError(422, "sealed_only",
                           "send the value sealed to the agent's readers (zks1) — never plaintext or zk1")
    if not may_answer(req, user):
        raise RequestError(403, "forbidden", "you cannot answer this request")
    with transaction.atomic():
        req = AgentSecretRequest.objects.select_for_update().get(pk=req.pk)
        if not live(req):
            raise RequestError(409, "request_closed", f"this request is {req.status if req.status != req.OPEN else 'expired'}")
        _declared(req.agent, req.name)
        if not req.agent.is_admin(user) and has_value(req.agent, req.name):
            raise RequestError(409, "value_exists",
                               f"{req.agent.slug} already has {req.name}; only its admins may replace it")
        try:
            zk.set_values(req.agent, kv=kv, values=[_Value(req.name, value_zk)], user=user)
        except ZkError as exc:
            raise RequestError(exc.status, exc.code, str(exc)) from exc
        req.status, req.fulfilled_by, req.fulfilled_at = req.FULFILLED, user, timezone.now()
        req.save(update_fields=["status", "fulfilled_by", "fulfilled_at"])


class _Value:
    """The `{name, value_zk}` shape `zk.set_values` reads (no `expect`)."""

    def __init__(self, name: str, value_zk: str):
        self.name, self.value_zk, self.expect = name, value_zk, None


def out(req) -> dict:
    return {"id": str(req.id), "agent": req.agent.slug, "name": req.name, "reason": req.reason,
            "status": req.status if req.status != req.OPEN or live(req) else "expired",
            "has_value": has_value(req.agent, req.name),
            "session_id": str(req.session_id) if req.session_id else None,
            "created_at": req.created_at, "expires_at": req.expires_at,
            "fulfilled_at": req.fulfilled_at,
            "fulfilled_by_email": req.fulfilled_by.email if req.fulfilled_by_id else ""}


# ---- the chat row that carries the seal card -------------------------------------------
# Decided server-side, like an MCP Apps row's `app`: a tool_result naming a request
# of THIS session (by `secret_request_id`, which only `request_agent_secret`
# returns) gets `secret_request` — on a reload (`annotate`) and on the live frame
# (`for_result_block`). A client never guesses from tool names.

_ID = re.compile(r'secret_request_id\\*"\s*:\s*\\*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})')


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _named_id(*sources) -> str | None:
    for s in _strings(list(sources)):
        if "secret_request_id" in s:
            m = _ID.search(s)
            if m:
                return m.group(1)
    return None


def _refs(session, ids) -> dict[str, dict]:

    if not ids or session is None:
        return {}
    return {str(r.id): {"id": str(r.id), "agent": r.agent.slug, "name": r.name}
            for r in AgentSecretRequest.objects.filter(pk__in=list(ids), session=session).select_related("agent")}


def annotate(session, messages) -> None:
    """Set `.secret_request` on each tool_result row naming a request of this session."""

    named = {}
    for m in messages:
        if getattr(m, "role", None) != Message.TOOL_RESULT or (getattr(m, "content", None) or {}).get("is_error"):
            continue
        rid = _named_id(getattr(m, "content", None), getattr(m, "plaintext", ""))
        if rid:
            named[id(m)] = rid
    refs = _refs(session, set(named.values()))
    for m in messages:
        ref = refs.get(named.get(id(m), ""))
        if ref:
            m.secret_request = ref


def for_result_block(session, block: dict) -> dict | None:
    """The live-frame form of `annotate`: one tool_result block arriving now."""
    if not block or block.get("is_error"):
        return None
    rid = _named_id(block)
    return _refs(session, {rid}).get(rid) if rid else None
