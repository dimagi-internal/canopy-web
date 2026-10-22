"""Grants (4.1): first-party per-agent grants, their reach over sources, and revocation."""
from __future__ import annotations

import datetime as dt
import json

from django.db import transaction
from django.utils import timezone

from ..models import Person, PersonFact, PersonGrant
from .consent import FEATURES, ON, memory_state, require_rollout, set_session_memory
from .core import (
    ACTIONS,
    HELD_CATEGORIES,
    SYSTEM,
    Actor,
    audit,
    denied,
    entry_urn,
    malformed,
    parse_entry_id,
    scope,
)
from .entries import _iso

# --- grants (4.1) ----------------------------------------------------------------


def client_of_turn(turn) -> tuple[str, str]:
    """(channel, host) a turn reached its agent through. The channel is how the
    person came in (`initiator_via`: slack, email, chat, mcp …); the host is
    the connected site when they came through an embedding SDK/widget."""
    channel = (getattr(turn, "initiator_via", "") or "").strip().lower()[:40]
    host = ""
    contact = getattr(turn, "initiator_contact", None) if getattr(turn, "initiator_contact_id", None) else None
    app = getattr(contact, "app", None) if contact is not None else None
    if app is not None:
        host = (getattr(app, "origin", "") or getattr(app, "name", "") or str(app.pk))[:200]
    return channel, host


def client_key(agent, channel: str, host: str, attributes: dict | None = None) -> str:
    parts = [f"agent={agent.slug if agent is not None else ''}", f"channel={channel}", f"host={host}"]
    for k in sorted(attributes or {}):
        parts.append(f"{k}={attributes[k]}")
    return "|".join(parts)[:400]


def client_name(agent, channel: str, host: str) -> str:
    """A name the person recognizes (4.1.5): "ACE over Slack", "Echo on example.org"."""
    who = agent.display_name if agent is not None else "canopy"
    where = f" on {host}" if host else (f" over {channel.capitalize()}" if channel else "")
    return f"{who}{where}"[:200]


def _expire_if_due(grant: PersonGrant) -> PersonGrant:
    if (grant.status == PersonGrant.ACTIVE and grant.expires_at is not None
            and grant.expires_at <= timezone.now()):
        PersonGrant.objects.filter(pk=grant.pk, status=PersonGrant.ACTIVE).update(
            status=PersonGrant.EXPIRED)
        grant.status = PersonGrant.EXPIRED
        audit(grant.person, "grant.expired", actor=SYSTEM, grant=grant,
              workspace_slug=grant.workspace_id, detail=grant.client_name)
    return grant


# --- first-party grants: per agent, and only by the person's act (4.1.4, 4.1.6) ---------
#
# Jonathan, 2026-10-09: "each agent session / entry should obtain the grant explicitly
# or due to previous granting to this agent." So:
#
# * The HCP CLIENT is the agent. A grant is keyed on the agent alone — not on how the
#   person reached it (channel) or where (host): "previous granting to this agent" is
#   what is reused. (`client_key(agent, "", "")`.)
# * The person's canopy-level settings (`Person.hcp_*_available/default`) are POLICY.
#   They bound what a grant may carry and what a session offers; they issue nothing.
# * A grant exists only because the person clicked one, in a session, after being shown
#   the agent, the categories, the actions and the duration (4.1.6). Two durations, two
#   separate acts with nothing preselected (4.1.4): "for this session" (temporary —
#   expires at `SESSION_GRANT_CAP` or when the session is archived, whichever is first)
#   and "always for <agent>" (persistent, until revoked).
# * Access for an operation = the session's switch for that feature (policy + this
#   session's choice, `effective`) AND a live grant to this agent carrying the scope.
#   Until granted, the agent behaves as if the feature were off; the UI does the asking.
# * Nothing is ever presumed. A widening (a feature the agent's grant does not carry)
#   is a new act and a new grant that replaces the old one.

SESSION_GRANT_CAP = dt.timedelta(hours=24)
TEMPORARY_FOR_SESSION, PERSISTENT_FOR_AGENT = "session", "always"
#: Where the person performed the act (`grant.issued` detail and `PersonGrant.modality`).
SURFACES = {"chat": "canopy-chat", "widget": "canopy-widget",
            # A contact's act in canopy's frame on a site trusted for email identity.
            "embed-trusted": "canopy-embed-trusted-email"}
_ACTION_OF = {"record": "write", "use": "read"}
_FEATURE_OF = {"write": "record", "read": "use"}


def feature_scopes(features) -> list[str]:
    """The scopes a grant for these features carries: write (record) and/or read
    (use) on every held category — custom categories never (4.1.1)."""
    actions = {_ACTION_OF[f] for f in features}
    return [scope(c, a) for c in HELD_CATEGORIES for a in ACTIONS if a in actions]


def features_of(grant: PersonGrant | None) -> set[str]:
    if grant is None:
        return set()
    return {_FEATURE_OF[a] for a in ACTIONS if categories_for(grant, a)}


def _agent_key(agent, session=None) -> str:
    return client_key(agent, "", "", {"session": str(session.pk)} if session is not None else None)


def _live(person: Person, agent, *, key: str, grant_type: str, session=None) -> PersonGrant | None:
    g = (PersonGrant.objects.select_related("person")
         .filter(person=person, client_key=key, workspace_id=agent.workspace_id,
                 grant_type=grant_type, status=PersonGrant.ACTIVE)
         .order_by("-issued_at", "-pk").first())
    if g is None:
        return None
    if session is not None and g.status == PersonGrant.ACTIVE and \
            getattr(session, "status", "") == "archived":
        # The session ended: its temporary grant lapses with it (4.1.4), never revived.
        PersonGrant.objects.filter(pk=g.pk, status=PersonGrant.ACTIVE).update(
            status=PersonGrant.EXPIRED)
        g.status = PersonGrant.EXPIRED
        audit(person, "grant.expired", actor=SYSTEM, grant=g, workspace_slug=g.workspace_id,
              detail=f"{g.client_name}: the session ended")
        return None
    g = _expire_if_due(g)
    return g if g.status == PersonGrant.ACTIVE else None


def agent_grants(person: Person, agent, session=None) -> list[PersonGrant]:
    """This agent's live grants for this person: the session's temporary one first,
    then the persistent one. Empty = never granted (or revoked, or lapsed)."""
    out = []
    if agent is None:
        return out
    if session is not None:
        g = _live(person, agent, key=_agent_key(agent, session), grant_type=PersonGrant.TEMPORARY,
                  session=session)
        if g is not None:
            out.append(g)
    g = _live(person, agent, key=_agent_key(agent), grant_type=PersonGrant.PERSISTENT)
    if g is not None:
        out.append(g)
    return out


def grant_for(person: Person, *, agent, session=None, action: str | None = None) -> PersonGrant | None:
    """The live grant this agent acts under for `action` ("read" / "write"; None =
    any). Never creates one."""
    for g in agent_grants(person, agent, session):
        if action is None or categories_for(g, action):
            return g
    return None


def granted(person: Person, agent, session=None) -> dict:
    """{record, use}: does this agent hold a live grant for each, in this session."""
    have = set()
    for g in agent_grants(person, agent, session):
        have |= features_of(g)
    return {f: f in have for f in FEATURES}


#: 4.1.5.1 `narrowedTo.sources` values: the person's personal entries, one
#: workspace's, or every workspace in an organisation's tree (its root and all its
#: divisions, as they are when the grant is used — a new division is covered
#: without a new grant).
#:
#: Reach is DERIVED from the relationship, never ticked in a prompt (Jonathan,
#: 2026-10-11: an agent serving me in another division "should have my Dimagi-wide
#: context and even personal context if I give it"). The building blocks are
#: CONTEXTS — an organisation (`tree:<root>`) and `personal` (what the person adds
#: themselves); a site is not one. A first-party agent's grant reaches its own
#: organisation's whole tree by default. To reach more (personal, say) the person
#: points that agent at one of their memory collections (`hcp_collections`), and
#: the grant then names `collection:<id>` plus the collection's sources.
PERSONAL_SOURCE = "personal"
COLLECTION_PREFIX = "collection:"


def workspace_source(slug: str) -> str:
    return f"workspace:{slug}"


def tree_source(root: str) -> str:
    return f"tree:{root}"


def root_of(slug: str | None) -> str | None:
    """The top of `slug`'s workspace tree (the organisation)."""
    if slug is None:
        return None
    from apps.workspaces.models import Workspace

    ws = Workspace.objects.filter(pk=slug).first()
    if ws is None:
        return slug
    ancestors = ws.ancestor_slugs()
    return ancestors[-1] if ancestors else ws.slug


def tree_slugs(root: str) -> set[str]:
    from apps.workspaces.services import descendant_slugs

    return {root} | descendant_slugs({root})


def agent_sources(agent) -> list[str]:
    """The default reach of a grant to `agent`: its OWN organisation's tree —
    every division, never another organisation's, never personal."""
    return [tree_source(root_of(agent.workspace_id))]


def org_name(agent) -> str:
    """The display name of the organisation an agent serves ("Dimagi")."""
    return _display_name(root_of(agent.workspace_id))


def collection_sources(collection) -> list[str]:
    """What a grant using `collection` names: the collection, then what it holds
    as resolved now (4.1.5.1 Rule 3 — the object says what it reaches)."""
    return [f"{COLLECTION_PREFIX}{collection.pk}", *collection.sources]


def default_reach(person: Person, agent) -> tuple[list[str], object]:
    """(sources, collection) a new grant to `agent` carries: the collection the
    person pointed this agent at (on its kept grant), else the default."""
    kept = _live(person, agent, key=_agent_key(agent), grant_type=PersonGrant.PERSISTENT)
    if kept is not None and kept.collection_id and kept.collection is not None:
        return collection_sources(kept.collection), kept.collection
    return agent_sources(agent), None


def restrictions_for(scopes: list[str], sources: list[str]) -> list[dict]:
    """4.1.5.1 Rule 3: every scope narrowed to the sources, so the grant object
    never reads as more than it reaches."""
    return [{"scope": s, "narrowedTo": {"sources": list(sources)}} for s in scopes]


def named_sources(grant: PersonGrant) -> list[str] | None:
    """The sources a grant's restrictions name (the first that names any; every
    scope of a grant canopy issues names the same ones), or None for none."""
    for r in grant.restrictions or []:
        named = (r.get("narrowedTo") or {}).get("sources")
        if named is not None:
            return [str(n) for n in named]
    return None


def _expand(sources) -> set:
    out: set = set()
    for n in sources:
        n = str(n)
        if n == PERSONAL_SOURCE:
            out.add(None)
        elif n.startswith("workspace:"):
            out.add(n.split(":", 1)[1])
        elif n.startswith("tree:"):
            out |= tree_slugs(n.split(":", 1)[1])
        # `collection:<id>` names where the sources came from; it reaches nothing itself.
    return out


def reached(sources) -> set:
    """The workspaces (None = personal) a list of sources reaches."""
    return _expand(sources)


def reach_of(grant: PersonGrant) -> set:
    """The sources (None = personal, else a workspace slug) a grant reaches —
    the one answer every read path asks. Read from its restrictions (Rule 1:
    several only intersect). A first-party agent's grant not using a collection
    is also bounded by its own organisation's tree, whatever it names; a row
    written before restrictions carried the reach is bounded by its workspace
    column."""
    reach: set | None = None
    for r in grant.restrictions or []:
        named = (r.get("narrowedTo") or {}).get("sources")
        if named is None:
            continue
        these = _expand(named)
        reach = these if reach is None else reach & these
    if grant.hcp_client_id:
        return reach if reach is not None else {None}
    if reach is None:
        return {grant.workspace_id}
    if grant.collection_id:
        return reach                       # the person chose it, through a collection
    return reach & tree_slugs(root_of(grant.workspace_id))


def _display_name(slug: str) -> str:
    from apps.workspaces.models import Workspace

    return Workspace.objects.filter(pk=slug).values_list("display_name", flat=True).first() or slug


def source_label(source: str) -> str:
    """How a person reads one source, saying only what it resolves to: "Connect",
    "all of Dimagi (every workspace in it, including new ones)" — a tree is
    expanded when read — and "entries in no workspace" (what you or an outside app
    wrote without one)."""
    if source == PERSONAL_SOURCE:
        return "entries in no workspace"
    kind, _, slug = source.partition(":")
    if kind == "tree":
        return f"all of {_display_name(slug)} (every workspace in it, including new ones)"
    return _display_name(slug)


def reach_names(grant: PersonGrant) -> list[str]:
    """What a grant reads RIGHT NOW, as workspace names, sorted ("No workspace"
    for entries in none) — the resolved form of `reach_label`."""
    slugs = reach_of(grant)
    names = sorted(_display_name(s) for s in slugs if s is not None)
    return names + (["No workspace"] if None in slugs else [])


def reach_label(sources) -> str:
    return " and ".join(source_label(s) for s in sources
                        if not str(s).startswith(COLLECTION_PREFIX)) or "nothing"


def _merge_sources(have: list[str], more: list[str]) -> list[str]:
    """The union, dropping a single workspace a named tree already covers. A
    collection named in `more` replaces one named in `have`."""
    if any(str(n).startswith(COLLECTION_PREFIX) for n in more):
        have = [n for n in have if not str(n).startswith(COLLECTION_PREFIX)]
    out = list(dict.fromkeys([*have, *more]))
    trees = {n.split(":", 1)[1] for n in out if n.startswith("tree:")}
    return [n for n in out
            if not (n.startswith("workspace:") and root_of(n.split(":", 1)[1]) in trees)]


def grant_sources(grant: PersonGrant | None) -> list[str]:
    """The sources a grant names; a pre-restriction agent row, its workspace."""
    if grant is None:
        return []
    named = named_sources(grant)
    if named is not None:
        return named
    return [workspace_source(grant.workspace_id)] if grant.workspace_id else []


def _disclosure(agent, features, sources, duration: str, expires_at) -> str:
    cats = ", ".join(HELD_CATEGORIES)
    acts = " and ".join(sorted(_ACTION_OF[f] for f in features))
    how = ("for this session, until " + _iso(expires_at) if duration == TEMPORARY_FOR_SESSION
           else f"always for {agent.display_name}, until you revoke it")
    return (f"shown: agent={agent.slug}; categories={cats}; actions={acts}; "
            f"reach=only {reach_label(sources)}; {how}")


def _record_grant(person: Person, *, agent, features, sources, gtype: str, actor: Actor,
                  surface: str, session=None, why: str = "", collection=None) -> PersonGrant:
    """Write the grant the person's act produced: replace a live grant of the same
    key with one carrying the union of features AND reach (a widening is its own
    act and its own `grant.issued`); asking for what is already held changes
    nothing."""
    if gtype == PersonGrant.TEMPORARY:
        key, expires_at = _agent_key(agent, session), timezone.now() + SESSION_GRANT_CAP
        current = _live(person, agent, key=key, grant_type=gtype, session=session)
        duration = TEMPORARY_FOR_SESSION
    else:
        key, expires_at = _agent_key(agent), None
        current = _live(person, agent, key=key, grant_type=gtype)
        duration = PERSISTENT_FOR_AGENT
    have = features_of(current)
    held = grant_sources(current)
    if current is not None and set(features) <= have and reached(sources) <= reached(held):
        return current
    want = sorted(have | set(features))
    reach = _merge_sources(held, sources)
    if current is not None:
        PersonGrant.objects.filter(pk=current.pk).update(status=PersonGrant.REVOKED,
                                                         revoked_at=timezone.now())
        audit(person, "grant.revoked", actor=actor, grant=current,
              workspace_slug=current.workspace_id,
              detail=f"{current.client_name}: replaced by a wider grant the person gave")
    grant = PersonGrant.objects.create(
        person=person, workspace_id=agent.workspace_id, agent=agent,
        attributes={"session": str(session.pk)} if gtype == PersonGrant.TEMPORARY else {},
        client_key=key, client_name=client_name(agent, "", ""),
        scopes=feature_scopes(want),
        restrictions=restrictions_for(feature_scopes(want), reach),
        grant_type=gtype, modality=SURFACES[surface], expires_at=expires_at,
        collection=collection or (current.collection if current is not None else None))
    audit(person, "grant.issued", actor=actor, grant=grant, workspace_slug=agent.workspace_id,
          detail=(f"client={grant.client_name}; key={key}; type={gtype}; "
                  f"modality={grant.modality} (the person's act){why}; "
                  f"expires={_iso(expires_at) or 'never'}; scopes={' '.join(grant.scopes)}; "
                  f"restrictions={json.dumps(grant.restrictions)}; "
                  f"{_disclosure(agent, want, reach, duration, expires_at)}"))
    return grant


@transaction.atomic
def issue_agent_grant(person: Person, *, agent, features, duration: str, actor: Actor,
                      session=None, surface: str = "chat", note: str = "") -> PersonGrant:
    """The person's act, in one of their sessions, for that session's agent.

    4.1.4: temporary is the default outcome, and persistence is a SEPARATE act that
    never also authorizes. So `duration=session` is the authorizing act (a temporary
    grant for this session, at most `SESSION_GRANT_CAP`); `duration=always` only
    ELECTS persistence for what this session's temporary grant already carries —
    asked for without one, it is refused. Bounded by policy: a feature not
    available is refused.

    The reach is derived, not chosen in the prompt (`default_reach`), and named
    on the grant (4.1.5.1); keeping it always keeps the reach this session's
    grant carries. Outside the rollout nothing is granted (`require_rollout`)."""
    require_rollout(person)
    features = [f for f in FEATURES if f in set(features or [])]
    if not features:
        raise malformed("name record and/or use")
    if duration not in (TEMPORARY_FOR_SESSION, PERSISTENT_FOR_AGENT):
        raise malformed("duration is session or always")
    if surface not in SURFACES:
        raise malformed("surface is " + ", ".join(SURFACES))
    if session is None:
        raise malformed("a grant is given in a session")
    for f in features:
        if not getattr(person, f"hcp_{f}_available"):
            raise denied(f"{f} is not available for you; make it available first")
    if duration == TEMPORARY_FOR_SESSION:
        sources, collection = default_reach(person, agent)
        grant = _record_grant(person, agent=agent, features=features, sources=sources,
                              gtype=PersonGrant.TEMPORARY, collection=collection,
                              actor=actor, surface=surface, session=session,
                              why=f", {note}" if note else "")
        # Allowing it in a session is also turning the feature on there, when the
        # person's default has it off ("only when the person turns it on in that session").
        state = memory_state(person, session)
        turn_on = {f: ON for f in features if not state[f]["effective"]}
        if turn_on:
            set_session_memory(person, session, actor=actor, **turn_on)
        return grant
    temp = _live(person, agent, key=_agent_key(agent, session), grant_type=PersonGrant.TEMPORARY,
                 session=session)
    if not set(features) <= features_of(temp):
        raise malformed("allow it for this session first; keeping it always is a separate choice")
    sources = grant_sources(temp)
    return _record_grant(person, agent=agent, features=features, sources=sources,
                         gtype=PersonGrant.PERSISTENT, collection=temp.collection,
                         actor=actor, surface=surface,
                         why=f", electing persistence after allowing it for this session "
                             f"(grant {entry_urn(temp.grant_id)})" + (f", {note}" if note else ""))


def categories_for(grant: PersonGrant, action: str) -> list[str]:
    out = []
    for s in grant.scopes or []:
        parts = s.split(":")
        if len(parts) >= 3 and parts[0] == "hcp" and parts[-1] == action:
            out.append(":".join(parts[1:-1]))
    return out


def allows(grant: PersonGrant | None, category: str, action: str) -> bool:
    return grant is not None and scope(category, action) in (grant.scopes or [])


def _restricted_entry_ids(grant: PersonGrant, category: str, action: str) -> set[str] | None:
    """4.1.5.1 Rule 1/4: a restriction only narrows. None = unrestricted."""
    ids: set[str] | None = None
    for r in grant.restrictions or []:
        if r.get("scope") != scope(category, action):
            continue
        named = (r.get("narrowedTo") or {}).get("entryIds")
        if named is None:
            continue
        found = {str(parse_entry_id(x)) for x in named if x}
        ids = found if ids is None else ids & found
    return ids


def within_grant(fact: PersonFact, grant: PersonGrant | None, action: str) -> bool:
    """Is this entry inside what the grant reaches? A first-party agent grant
    reaches its own workspace; an OAuth client's grant reaches the sources its
    restriction names (the person's personal entries, plus workspaces they chose)."""
    if grant is None:
        return False
    if fact.workspace_id not in reach_of(grant):
        return False
    if not allows(grant, fact.category, action):
        return False
    ids = _restricted_entry_ids(grant, fact.category, action)
    return ids is None or str(fact.entry_id) in ids


@transaction.atomic
def revoke_grant(grant: PersonGrant, *, actor: Actor) -> PersonGrant:
    """4.2: effective immediately. A first-party grant has no token to chase —
    every read checks the grant row. An OAuth client's grant also has its tokens
    killed and its webhook notified (`hcp_oauth.revoke`)."""
    if grant.hcp_client_id:
        from .. import hcp_oauth

        return hcp_oauth.revoke(grant, actor=actor, reason="revoked by the person")
    if grant.status == PersonGrant.ACTIVE:
        PersonGrant.objects.filter(pk=grant.pk).update(status=PersonGrant.REVOKED,
                                                       revoked_at=timezone.now())
        grant.refresh_from_db()
        audit(grant.person, "grant.revoked", actor=actor, grant=grant,
              workspace_slug=grant.workspace_id, detail=grant.client_name)
    return grant


def grant_dict(grant: PersonGrant) -> dict:
    client = grant.hcp_client if grant.hcp_client_id else None
    return {
        "grantId": entry_urn(grant.grant_id),
        "client": ({"id": client.client_id, "name": client.name} if client is not None
                   else {"id": grant.client_key, "name": grant.client_name}),
        "scopes": list(grant.scopes or []),
        "restrictions": list(grant.restrictions or []),
        "grantType": grant.grant_type,
        "grantor": grant.grantor or None,
        "issuedAt": _iso(grant.issued_at),
        "expiresAt": _iso(grant.expires_at),
        "status": grant.status,
        # canopy's own: the dimensions the client key is built from.
        "canopy": {"agent": grant.agent.slug if grant.agent_id else None,
                   "channel": grant.channel, "host": grant.host,
                   "workspace": grant.workspace_id, "modality": grant.modality,
                   # 4.1.5.1 Rule 3: the reach, in the words a person reads.
                   "reach": reach_label(grant_sources(grant)) if not grant.hcp_client_id else "",
                   # The same reach as data: the workspaces it reads (null = your
                   # personal entries), so a page can show WHICH entries it sees
                   # without re-deriving the tree.
                   "reaches": sorted(reach_of(grant), key=lambda s: (s is not None, s or "")),
                   "reach_names": reach_names(grant),
                   # The person's memory collection it uses (None = the agent's default).
                   "collection": ({"id": grant.collection_id, "name": grant.collection.name}
                                  if grant.collection_id else None),
                   "client": ({"id": client.client_id, "name": client.name,
                               "operator": client.operator} if client is not None else None)},
    }
