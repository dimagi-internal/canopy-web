"""The caller envelope: who asked for a turn, and what canopy knows about them.

Its vocabulary — `relationship` (owner | admin | member | contact | system),
`profile` (full | confined), `granted_by`, `turn_mode`, `ship_grant` — is defined
in `docs/architecture/access.md`; the decision behind `profile` and `granted_by`
is `apps/agents/access.decide`.

Phase 1b of `docs/superpowers/specs/2026-09-18-who-is-asking-initiator-identity-
and-access-design.md` (§5). Phase 1a RECORDED the initiator on every turn; this
DELIVERS it, so the agent answering an email knows who it is answering and how
sure canopy is, instead of re-deriving that from a `From:` header anyone can
write.

**Data, never prompt.** The runner writes this beside the turn and the agent
reads it (and re-reads it mid-turn with the `who_is_asking` MCP tool). It is
never pasted into the prompt: a chat turn's prompt is the person's own words and
becomes the transcript, so anything prepended would read as something they typed.

**Informs, does not enforce.** The envelope is what the agent's judgement runs
on. Enforcement — what a caller may make the agent DO — is a later phase, and
nothing here should be read as a grant.

**`verified` is about THIS message.** It reads the turn's own assurance, never
the contact's best-ever grade: a forged message from an address that once passed
DMARC is still forged.
"""
from __future__ import annotations

import re

from apps.contacts.models import Contact

from . import initiator as who

#: Bump when a field's MEANING changes; adding a field does not need it.
#: 2 (2026-10-04): `relationship` "caller" is now "contact", `profile`
#: "restricted" is now "confined" — one word per meaning, the same words the
#: roster and the docs use. Readers accept both (`normalize_relationship`,
#: `normalize_profile`), since an envelope written by an older canopy-web may
#: still sit on a box.
#: 3 (2026-10-07, fleet brain v1, canopy#804): adds `person` — what canopy knows
#: about the human asking (facts + digest, this agent's workspace only) — and
#: `trigger.kind`. Purely additive: every v2 field keeps its meaning, so a v2
#: reader (runner, canopy hook) ignores the new keys and works unchanged. The
#: runner gate (`routing.ENVELOPE_VERSION`) stays 2 for that reason.
VERSION = 3

#: User assurances that establish the person, not just a claim about them.
#: `dmarc` is a member resolved from a DMARC-aligned email (harness
#: `_member_behind_email`), which is only ever done on THIS message's grade.
#: `slack_email` is a full member of our own Slack matched by the profile email
#: that Slack's owning organisation provisioned (`slack.services.auto_link`
#: refuses guests and other teams' members).
_VERIFIED_USER = frozenset({who.SESSION, who.PAT, who.DELEGATED, who.SLACK_LINKED, who.SLACK_EMAIL,
                            who.APPROVAL, Contact.AUTH_DMARC, Contact.AUTH_DKIM_ALIGNED})

#: Relationships, strongest first — the agent roles of docs/architecture/access.md.
#: `contact` is anyone who is not a member of the agent's workspace (an emailer,
#: a widget visitor, a canopy user from another tenant, someone unidentified).
OWNER, ADMIN, MEMBER, CONTACT, SYSTEM = "owner", "admin", "member", "contact", "system"
#: `profile` values: the agent's whole profile, or one capability of its interface.
FULL_PROFILE, CONFINED_PROFILE = "full", "confined"

#: Envelope values from VERSION 1, and what they are called now.
_LEGACY_RELATIONSHIP = {"caller": CONTACT}
_LEGACY_PROFILE = {"restricted": CONFINED_PROFILE}


def normalize_relationship(value) -> str:
    """A `relationship` as VERSION 2 spells it (`caller` -> `contact`)."""
    v = str(value or "")
    return _LEGACY_RELATIONSHIP.get(v, v)


def normalize_profile(value) -> str:
    """A `profile` as VERSION 2 spells it (`restricted` -> `confined`)."""
    v = str(value or "")
    return _LEGACY_PROFILE.get(v, v)


def _agent_of(turn):
    if turn.agent_id:
        return turn.agent
    cs = getattr(turn, "chat_session", None)
    return cs.agent if cs is not None and cs.agent_id else None


def _verified(turn) -> bool:
    kind, grade = turn.initiator_kind, turn.initiator_assurance or ""
    if kind == who.USER:
        return grade in _VERIFIED_USER
    if kind == who.CONTACT:
        # Tier 3: the signature is tied to the identity the reader sees, which
        # for a contact means DMARC-aligned (or domain-signed) MAIL, or a full
        # member of our own Slack (`slack_member`). A visitor from an embedded site tops out at tier 2 by design and
        # is therefore never verified here — the host vouches for them, and
        # canopy checks the host's signature, not the human. Gate an embedded
        # agent's interface on `contact`, never `contact:verified`.
        rank = Contact.AUTH_RANK.get(grade, 0)
        return rank >= Contact.AUTH_RANK[Contact.TIER_SIGNED_ALIGNED]
    # canopy itself, or another agent: nobody outside asserted anything.
    return kind in (who.SYSTEM, who.AGENT)


def relationship(turn, agent) -> str:
    """Owner, admin, member, contact or system — what this asker IS to the agent."""
    kind = turn.initiator_kind
    if kind in (who.SYSTEM, who.AGENT):
        return SYSTEM
    user = turn.initiator_user if kind == who.USER else None
    if user is None:
        return CONTACT
    if agent is None:
        return _relationship_without_agent(turn, user)
    return relationship_for_user(user, agent)


def _relationship_without_agent(turn, user) -> str:
    """What someone is to a turn with NO agent — a repo chat, a project turn.

    There is no agent to own, so the question is whose conversation and whose
    machine it is. It used to fall through to a CALLER (now `contact`), so the owner of a repo
    chat on their own laptop was told, in the "who is asking" note on every
    message, that they did not hold the agent's authority and must not push or
    deploy (2026-09-27, on Jonathan's own canopy-web session).

    OWNER: the runner's owner doing the work (it is their box and
    their Claude login), or the session's owner by the session ACL. MEMBER:
    anyone else the session ACL lets write. Everyone else is a CONTACT.
    """
    runner = getattr(turn, "claimed_by", None)
    if runner is not None and getattr(runner, "owner_id", None) == user.pk:
        return OWNER
    # An agent's OWN login (`Agent.user`, #983) working on a box its OWNER paired is
    # that agent acting where its owner's authority already runs — the dispatch shape
    # (an agent session asked by its owner to start work on the owner's runner, calling
    # canopy with the agent's PAT). It fell through to CALLER (now contact) and froze push/merge in
    # exactly the sessions the owner asked for (canopy-web#1011). Same answer
    # `relationship_for_user` gives the agent's own login; still only its own, and
    # only on its owner's box — anywhere else it stays a CONTACT.
    agent_self = getattr(user, "agent_identity", None)
    owner = getattr(runner, "owner_id", None) if runner is not None else None
    if agent_self is not None and owner is not None and agent_self.owner_id == owner:
        return SYSTEM
    session = getattr(turn, "chat_session", None)
    if session is None:
        return CONTACT
    from apps.canopy_sessions import access
    from apps.canopy_sessions.models import SessionParticipant

    role = access.role_for(user, session)
    if role == SessionParticipant.OWNER:
        return OWNER
    return MEMBER if access.can_write(user, session) else CONTACT


def relationship_for_user(user, agent) -> str:
    """What a canopy USER is to the agent, with no turn in hand (e.g. listing
    the MCP tools they may call)."""
    if agent is None or not getattr(user, "is_authenticated", False):
        return CONTACT
    from apps.workspaces import services as wsvc

    # Owner only while still in the tenant: an owner removed from the workspace
    # who messages the agent (Slack, email) is an outsider now, and must reach
    # it through its interface like one — not with an unconfined profile.
    if agent.owner_id == user.pk and wsvc.is_member(user, agent.workspace_id):
        return OWNER
    # The agent's OWN canopy login (`Agent.user`, #983) is the agent itself —
    # confining it against itself is nonsense. Only its own: another agent's
    # login stays whatever its membership makes it, or everyone with the whole
    # of agent A could steer agent B through A (anyone at dimagi.com with full
    # ACE shipping code through Hal).
    if agent.user_id is not None and agent.user_id == user.pk:
        return SYSTEM
    is_admin = getattr(agent, "is_admin", None)
    if callable(is_admin) and is_admin(user):
        return ADMIN
    return MEMBER if wsvc.is_member(user, agent.workspace_id) else CONTACT


def _contact(contact) -> dict | None:
    if contact is None:
        return None
    return {
        "id": contact.pk,
        "email": contact.email,
        "display_name": contact.display_name,
        "source": contact.source,
        # THIS message's grade and the best ever seen, side by side: a drop is
        # the signal worth noticing (see Contact.last_auth_result).
        "this_message_grade": contact.last_auth_result,
        "best_grade": contact.auth_result,
        "notes": contact.notes,
        "attributes": contact.attributes or {},
        "message_count": contact.message_count,
        "first_seen_at": contact.first_seen_at.isoformat() if contact.first_seen_at else None,
        "is_blocked": contact.is_blocked,
    }


#: The message grades that would have tied the sender to their account.
_ALIGNED_NEEDS = (Contact.AUTH_DMARC, Contact.AUTH_DKIM_ALIGNED)


#: The TurnEvent kind that logs `unproven_member` on the turn (`ledger.append_events`).
#: Written by canopy at enqueue, never accepted from a runner: it is not in
#: `harness.api.ALLOWED_EVENT_KINDS`. (`TurnEvent.kind` holds 20 characters.)
UNPROVEN_MEMBER_EVENT = "unproven_member"


def unproven_member(turn) -> dict | None:
    """`unproven_member` for a turn on its own — what the envelope carries."""
    return _unproven_member(turn, _agent_of(turn))


def _unproven_member(turn, agent) -> dict | None:
    """The member an unaligned email's address belongs to, or None.

    Only for an email turn whose initiator stayed a CONTACT (not blocked) because
    this message's grade is not in `Contact.EMAIL_ALIGNED`, where the address is
    held by exactly one canopy user who is a member of the agent's workspace —
    the same lookup `_member_behind_email` links with (`address_holder`). Reads
    the turn's own grade, never the contact's best: the question is about THIS
    message. Informs, does not enforce.
    """
    if agent is None or turn.origin != "email" or turn.initiator_kind != who.CONTACT:
        return None
    contact = turn.initiator_contact if turn.initiator_contact_id else None
    if contact is None or contact.is_blocked or not contact.email:
        return None
    grade = turn.initiator_assurance or Contact.AUTH_NONE
    if grade in Contact.EMAIL_ALIGNED:
        return None
    from .services import address_holder

    user, role = address_holder(contact.email, agent.workspace_id)
    if user is None or role is None:
        return None
    # Linked to a different account already: that is not this member's address.
    if contact.user_id is not None and contact.user_id != user.pk:
        return None
    return {
        "email": contact.email,
        "role": role,
        "this_message_grade": grade,
        "needs": list(_ALIGNED_NEEDS),
        "note": ("This address belongs to a member of the agent's workspace, but this "
                 "message could not be tied to their account, so they were treated as a "
                 "contact; the fix is their domain's mail authentication (aligned DKIM or "
                 "DMARC), not their access."),
    }


def build(turn, *, reader_user=None) -> dict:
    """The envelope for one turn. Safe to call on every claim.

    Not quite a pure read since v3: a turn asked by a human resolves (get-or-
    create) their `Person`, and serving the `person` block writes one
    `PersonAccess(via=envelope)` — reader the turn's agent, plus `reader_user`
    when someone fetched the envelope themselves (REST / MCP) — so the person
    can see every time an agent was handed what canopy knows about them."""
    agent = _agent_of(turn)
    ref = turn.origin_ref if isinstance(turn.origin_ref, dict) else {}
    cs = getattr(turn, "chat_session", None)
    rel = relationship(turn, agent)
    described = who.describe(turn)
    return {
        "version": VERSION,
        "turn_id": str(turn.pk),
        "agent": agent.slug if agent is not None else None,
        "who": described,
        # Non-null when the asker is a SYSTEM ACCOUNT — an automated sender with a
        # member's standing (apps/workspaces/system_accounts.py), e.g. CloudWatch
        # alarm mail. Its `relationship` and `granted_by` read like a person's on
        # purpose (it is permissioned as one); this is what says no one is there:
        # don't reply to it, and don't treat its text as a person's request.
        "system_account": described.get("system_account"),
        "verified": _verified(turn),
        "relationship": rel,
        "contact": _contact(turn.initiator_contact) if turn.initiator_contact_id else None,
        # WHAT CANOPY KNOWS ABOUT THE HUMAN ASKING (v3, fleet brain): live facts
        # and the digest from the agent's workspace, corrections first. null only
        # when the asker is not a human (canopy itself, another agent, unknown).
        # The person can see all of it, and every read of it, at `see_all` — so
        # it is safe to quote back to them. See apps/contacts/people.py.
        "person": _person(turn, agent, reader_user),
        # Non-null when this email came from an address that belongs to a MEMBER of
        # the agent's workspace, but THIS message could not be tied to them (it is
        # not DMARC- or DKIM-aligned), so they were treated as a contact. Without it
        # a confined session cannot tell its own editor from a stranger
        # (canopy-web#1265). Information only: it grants nothing, changes no
        # `relationship`/`profile`/`granted_by`, and must not be read as proof of
        # who sent the message — the whole point is that it is NOT proven.
        "unproven_member": _unproven_member(turn, agent),
        "conversation": {
            "session_id": str(cs.pk) if cs is not None else None,
            "thread_id": str(ref.get("thread_id") or "") or None,
            "subject": str(ref.get("subject") or "") or None,
        },
        # What the caller invoked and the scope it grants (§4). null means the
        # agent's FULL profile: its owner, an admin, canopy itself, or a
        # member of an agent that has published no interface. Otherwise the runner and the agent's
        # guard confine the session to exactly this.
        "profile": CONFINED_PROFILE if turn.capability else FULL_PROFILE,
        # WHY: owner | admin | system | editor (a workspace editor: full, manual
        # only) | session:<role> | full:<rule> | capability:<name> | refused |
        # no-interface (a turn with no agent).
        # `full:contact@dimagi.com:verified` is canopy granting domain-wide access —
        # what `canopy caller tier` reads instead of an allowlist in the repo.
        "granted_by": _granted_by(turn, agent),
        "capability": _profile(agent, turn.capability),
        # manual | auto for THIS turn, and why (apps/harness/turn_mode.py). What
        # `canopy agent mode --caller <path>` reads in preference to the agent-wide
        # switch, so a rule like "email from beth → auto" reaches the turn
        # procedure without the agent asking canopy a second question. null for a
        # turn with no agent.
        "turn_mode": _turn_mode(turn),
        # The owner-approved REPO-INTERNAL SHIP GRANT (Jonathan, 2026-10-03): when
        # another agent's login that holds this agent's keys (an explicit admin, or
        # the owner) dispatches work AT this agent, the session may push, open PRs
        # and merge in THIS AGENT'S OWN repo without stopping for the owner. Nothing
        # else — mail, publishing, public writes, deploys, other systems' state, and
        # every other repo stay exactly where `turn_mode` puts them. null otherwise.
        # See `_ship_grant` for the conditions; an envelope without the field (an
        # older canopy-web) means no grant.
        "ship_grant": _ship_grant(turn, agent, rel),
        # WHY THIS TURN EXISTS, not just who sent it: which message, found how, and
        # what already ran on the thread. Without it a confined session can't tell a
        # new message from a re-fire, or say whether a Gmail filter would have stopped
        # it (both push and poll run the runner's `in:inbox is:unread` query). ace@
        # thread 1a0d0a1632cfde4f: 14 `ask` sessions on SES receipts, each guessing.
        "trigger": _trigger(turn, ref),
        "thread_history": _thread_history(turn, ref),
        # WHAT THE PERSON IS LOOKING AT, when the conversation is embedded in a
        # page that declared its state: the SELECTION (ids + filters, already
        # capped at 8 KiB), never the rows — the agent reads those with the
        # page's backing tool. The canopy plugin's UserPromptSubmit hook puts
        # this into the session's context, which is not a transcript row, so
        # the agent has the screen on turn one even while its MCP servers are
        # still connecting (why the widget used to paste it) and the person's
        # message stays exactly what they typed. Until 2026-09-26 the widget
        # pasted it, and every transcript showed a JSON dump under the question.
        "page": _page(cs),
    }


def _person(turn, agent, reader_user) -> dict | None:
    from apps.contacts import people

    if agent is not None:
        ws = agent.workspace_id
    elif getattr(turn, "chat_session", None) is not None:
        ws = turn.chat_session.workspace_id
    else:
        ws = turn.workspace_id
    return people.envelope_block(turn, agent=agent, workspace_slug=ws, reader_user=reader_user)


def _page(session) -> dict | None:
    state = (getattr(session, "page_state", None) or {}) if session is not None else {}
    if not isinstance(state, dict) or not state.get("resource"):
        return None
    ids = state.get("visible_ids")
    backing = state.get("backing_tool") or state.get("backing_tools") or None
    return {
        "resource": str(state["resource"])[:500],
        "visible_ids": ids if isinstance(ids, list) else [],
        "visible_count": len(ids) if isinstance(ids, list) else None,
        "filters": state.get("filters") if isinstance(state.get("filters"), dict) else None,
        "backing_tool": backing,
        "path": str(state.get("path") or "")[:500] or None,
        "read_with": "current_page",
    }


_COUNT_SUFFIX = re.compile(r"-(\d+)$")


def _trigger(turn, ref: dict) -> dict:
    count = ref.get("message_count")
    if count is None and turn.origin == "email":
        # The email idempotency key is `email-<agent>-<thread>-<messageCount>`, so
        # turns enqueued before the runner sent `message_count` still say it.
        m = _COUNT_SUFFIX.search(turn.idempotency_key or "")
        count = int(m.group(1)) if m else None
    runner = turn.claimed_by if turn.claimed_by_id else None
    return {
        "origin": turn.origin,
        # What canopy-initiated work this turn IS, when it is one (v3):
        # "people_digest" for a digest turn (apps/harness/people_digest.py).
        "kind": str(ref.get("trigger") or "") or None,
        "discovered_by": ref.get("discovered_by"),
        "from": ref.get("from"),
        "message_id": ref.get("message_id"),
        "message_count": count,
        "runner": runner.name if runner is not None else None,
        "matched_classes": _matched_classes(turn),
    }


def _matched_classes(turn) -> list[str]:
    """The caller classes that earned this turn its capability (e.g. `contact:verified`)
    — the interface clause an operator would edit to change who gets `ask`."""
    agent = _agent_of(turn)
    if agent is None or not turn.capability:
        return []
    from apps.agents.interface import caller_classes

    iface = getattr(agent, "interface", None) or {}
    cap = (iface.get("capabilities") or {}).get(turn.capability) or {}
    classes = caller_classes(turn, relationship(turn, agent))
    return sorted(classes & set(cap.get("callers") or []))


def _thread_history(turn, ref: dict) -> dict | None:
    tid = ref.get("thread_id")
    agent = _agent_of(turn)
    if not tid or agent is None:
        return None
    from django.db.models import Q

    from .models import Turn

    # An email turn targets the thread's SESSION, not the agent (the two are exclusive
    # by constraint), so match the agent either way.
    prior = (Turn.objects.filter(Q(agent_id=agent.pk) | Q(chat_session__agent_id=agent.pk),
                                 origin_ref__thread_id=tid, created_at__lt=turn.created_at)
             .exclude(pk=turn.pk).order_by("-created_at"))
    last = prior.first()
    return {
        "prior_turns": prior.count(),
        "last_prior_turn_id": str(last.pk) if last is not None else None,
        "last_prior_message_count": (
            _trigger(last, last.origin_ref if isinstance(last.origin_ref, dict) else {})
            ["message_count"] if last is not None else None),
    }


#: What a ship grant covers, by name, and what it pointedly does not. Both travel in
#: the envelope so the receiving session never has to infer the boundary.
SHIP_ACTIONS = ("push", "pull_request", "merge")
SHIP_NOT_GRANTED = ("send email or messages", "publish or share documents", "public writes",
                    "deploy or change other systems' state", "spend",
                    "any repo other than the one named")
SHIP_NOT_GRANTED_STANDING = SHIP_NOT_GRANTED[:-1] + ("any repo other than the ones named",)


def _ship_grant(turn, agent, rel: str) -> dict | None:
    """The ship grant this turn carries, or None: the per-dispatch grant
    (`_dispatch_grant`), the owner's standing one (`_standing_grant`), or both
    merged. `repo` stays the first repo so an older canopy hook that reads only
    that key still sees a valid grant; `repos` is the whole list."""
    dispatch = _dispatch_grant(turn, agent, rel)
    standing = _standing_grant(turn, agent, rel)
    if dispatch is None:
        return standing
    if standing is not None:
        dispatch["repos"] = list(dict.fromkeys(dispatch["repos"] + standing["repos"]))
        dispatch["basis"] = f"{dispatch['basis']}; {standing['basis']}"
    return dispatch


def _standing_grant(turn, agent, rel: str) -> dict | None:
    """The owner's STANDING ship grant (`Agent.ship_repos`), or None.

    Jonathan, 2026-10-08: a scheduled Eva turn found and tested a one-line fix,
    then held the push for approval because `manual` files push / merge beside
    send / publish — "I didn't intend that design". The agent's repo already said
    "ship code freely; the PR is not a review gate", but a repo cannot grant
    itself autonomy, so the owner states it here instead. Every condition:

    * the turn targets an AGENT (`turn.agent`) whose owner listed repos;
    * the caller holds the agent's own authority on a verified basis — its
      OWNER, an ADMIN, or SYSTEM (a schedule, canopy itself, the agent's own
      login). Never a member or a contact: their say-so must not merge code;
    * not the anonymous `kind=agent` an approved item records — it names a
      slug, not a login canopy authenticated (same exclusion as the dispatch
      grant).
    """
    if agent is None or not turn.agent_id:
        return None
    repos = [r for r in (agent.ship_repos or []) if isinstance(r, str) and _REPO.match(r)]
    if not repos:
        return None
    if rel not in (OWNER, ADMIN, SYSTEM) or not _verified(turn):
        return None
    if turn.initiator_kind == who.AGENT:
        return None
    owner = getattr(agent.owner, "email", "") or "the owner"
    return {
        "repo": repos[0],
        "repos": repos,
        "actions": list(SHIP_ACTIONS),
        "dispatched_by": None,
        "basis": f"standing grant set on {agent.slug} (owner {owner})",
        "not_granted": list(SHIP_NOT_GRANTED_STANDING),
    }


_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")


def _dispatch_grant(turn, agent, rel: str) -> dict | None:
    """The repo-internal ship grant for an agent-to-agent dispatch, or None.

    Owner decision, 2026-10-03 (Jonathan): Ada's fix dispatches to its sibling
    agents (eva#343, eva#347, canopy#715) each stopped for the owner to type "yes
    merge" although the brief said to merge — the receiving session read
    `turn_mode: manual` and, correctly by its envelope, waited. The grant lifts
    THAT wait and only that one. Every condition is required:

    * the turn targets an AGENT directly (`turn.agent`) — not a repo/project
      turn, and not a chat or email session that happens to belong to one;
    * it was asked by a canopy USER on a verified credential for this request
      (a PAT or a signed-in session) — never a contact, an unverified grade, or
      the anonymous `kind=agent` an approved item records (which names a slug,
      not a login canopy authenticated);
    * that user is ANOTHER agent's own login (`Agent.user`) — a human is out of
      scope for v1, and an agent dispatching itself is `system`, not a grant;
    * that login is the target's OWNER or an ADMIN (`Agent.is_admin`, so a
      workspace owner's agent login counts; `editor` membership does not);
    * the target names a GitHub repo (`repo_url`) — the grant is scoped to it,
      so with no repo there is nothing to grant.
    """
    if agent is None or not turn.agent_id:
        return None
    if turn.initiator_kind != who.USER or not _verified(turn):
        return None
    if rel not in (OWNER, ADMIN):
        return None
    user = turn.initiator_user
    dispatcher = getattr(user, "agent_identity", None) if user is not None else None
    if dispatcher is None or dispatcher.pk == agent.pk:
        return None
    from apps.agents.delegations import agent_repo

    repo = agent_repo(agent)
    if not repo:
        return None
    return {
        "repo": repo,
        "repos": [repo],
        "actions": list(SHIP_ACTIONS),
        "dispatched_by": {"email": user.email, "agent": dispatcher.slug},
        "basis": f"dispatched by {user.email} (agent {dispatcher.slug}), {rel} of {agent.slug}",
        "not_granted": list(SHIP_NOT_GRANTED),
    }


def _turn_mode(turn) -> dict | None:
    from .turn_mode import for_turn

    resolved = for_turn(turn)
    return None if resolved is None else {"mode": resolved.mode, "basis": resolved.basis}


def _granted_by(turn, agent) -> str:
    if agent is None:
        return "no-interface"
    from apps.agents.interface import granted_by

    return granted_by(turn, agent)


def _profile(agent, capability: str):
    if agent is None or not capability:
        return None
    from apps.agents.interface import profile

    return profile(agent, capability)
