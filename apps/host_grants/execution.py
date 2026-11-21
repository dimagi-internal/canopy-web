"""What canopy states to a host about the call in front of it — `Canopy-Execution`.

The gateway (`host_gateway`) is the one place that can speak for a call: it runs in
canopy-web, holds the visitor's DPoP-bound token, and never hands it to a runner.
So on every request it makes to a host on an agent's turn it adds a statement,
signed by canopy's published client key, saying:

| claim | value |
| --- | --- |
| `agent` | slug |
| `capability` | the profile the turn runs in (read mode's stored name, or a host site's) |
| `declaration` | `{data, capability, declared_by, declared_at}` — the host site's `data: sensitive` setting and who last published the interface holding it — or null |
| `runner` | `{id, name, flags: [{flag, declared_by, declared_at}]}` — flags IN FORCE |
| `runner_posture` | the latest heartbeat's `data_mode` and config hash |
| `requirements` | the conversation's requirements |
| `turn`, `session` | ids |
| `sub` | the host's own id for the person, when the turn's initiator is one of the host's contacts (`subject_of`); absent otherwise |

plus `iss` (canopy's client_id), `aud` (the host's MCP resource), `iat`/`exp` (60s),
`jti` (single use), `cnf.jkt` (canopy's DPoP key) and `ath` (the access token).

The facts are read once per gateway call (`facts_for`); each HTTP request gets its
own signed statement (`host_gateway._auth`). The host verifies with
`canopy_sdk.host.verify_execution` and decides with `Execution.sensitive_clearance`.
"""
from __future__ import annotations

from apps.agents import sensitive_data
from apps.harness import runner_requirements as rr


def _person(user) -> str:
    return getattr(user, "email", "") if user is not None else ""


def runner_facts(runner) -> dict:
    """The runner as canopy states it: its id, and each flag IN FORCE with who
    declared it and when (a declared flag the box contradicts is not stated)."""
    if runner is None:
        return {"id": "", "name": "", "flags": []}
    live = rr.in_force(runner)
    flags = [{"flag": f.flag, "declared_by": _person(f.declared_by),
              "declared_at": f.declared_at.isoformat() if f.declared_at else ""}
             for f in runner.declared_flags.select_related("declared_by").order_by("flag")
             if f.flag in live]
    return {"id": str(runner.pk), "name": runner.name, "flags": flags}


def posture_facts(runner) -> dict:
    caps = (getattr(runner, "capabilities", None) or {}) if runner is not None else {}
    return {"data_mode": str(caps.get(rr.DATA_MODE_KEY) or ""),
            "config_hash": str(caps.get(rr.DATA_MODE_HASH_KEY) or "")}


def subject_of(turn, app) -> str:
    """The host's own id for the person whose turn this is, when they are one of `app`'s own
    contacts (`Contact.external_id`, keyed by that site), else "". Read from the turn's
    initiator, never from anything the agent sent."""
    from apps.contacts.models import Contact  # noqa: PLC0415 — contacts reads harness: loads with a call

    contact = turn.initiator_contact if getattr(turn, "initiator_contact_id", None) else None
    if contact is None or app is None or contact.app_id != app.pk or contact.source != Contact.SOURCE_EMBED:
        return ""
    return contact.external_id or ""


def facts_for(turn, agent, session, requirements, *, app=None) -> dict:
    """Everything a statement for this turn says, before it is signed. With `app` (the site
    the call goes to), `sub` too when the turn's initiator is one of that site's own contacts
    (`subject_of`): the host can then hold every call to that one person."""
    facts = {
        "agent": agent.slug,
        "capability": turn.capability or "",
        "declaration": sensitive_data.as_claim(agent, turn.capability),
        "runner": runner_facts(turn.claimed_by),
        "runner_posture": posture_facts(turn.claimed_by),
        "requirements": sorted(requirements),
        "turn": str(turn.pk),
        "session": str(session.pk) if session is not None else "",
    }
    sub = subject_of(turn, app)
    if sub:
        facts["sub"] = sub
    return facts
