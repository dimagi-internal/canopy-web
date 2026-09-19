"""Who asked for a turn, and how sure we are — one shape for every channel.

Phase 1 of `docs/superpowers/specs/2026-09-18-who-is-asking-initiator-identity-
and-access-design.md`. RECORD-ONLY: nothing here changes what a turn may do yet.
It exists so the rest can: an agent cannot act for someone, and a tool cannot be
limited to someone, until every turn says who that someone is.

**Why one shape.** Before this, "who" was scattered and partial. `enqueued_by`
was the only field, and the generic enqueue endpoint stamped it with the CALLER
— so an email posted by a runner was recorded as "launched by" whoever paired
that runner, not by the stranger who wrote it. An email sender lived untyped in
`origin_ref["from"]`; a schedule said nothing about the person who set it up; a
contact on the widget left no trace on the turn at all. A consumer asking "who
is this for?" had to know every channel's private convention.

**Why assurance travels with it.** "Alice" from a signed-in browser, "Alice" by
a host's signature, and "Alice" by an email with no DKIM are three different
claims. The access policy (a later phase) must be able to treat them
differently, and the agent should be told which one it has.

**`unknown` is a real value, and a test fails if production produces it.** A
default that guessed would be worse than no field: every consumer would read a
confident answer that nobody established.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --- kind: WHAT asked --------------------------------------------------------
USER = "user"          # a canopy account
CONTACT = "contact"    # someone canopy knows who is not a member (apps/contacts)
SYSTEM = "system"      # canopy itself — a schedule firing, a drill
AGENT = "agent"        # another agent dispatching work
UNKNOWN = "unknown"    # nobody established it; see the module docstring

KINDS = (USER, CONTACT, SYSTEM, AGENT, UNKNOWN)

# --- assurance: HOW we know --------------------------------------------------
# Users. Ordered roughly strongest-first, but deliberately not ranked here: what
# each is worth is the access policy's call, per agent, not this module's.
SESSION = "session"            # signed in to canopy in this very request
PAT = "pat"                    # a personal access token (a machine acting as its owner)
DELEGATED = "delegated"        # a token minted FOR the user by an app (the widget)
SLACK_LINKED = "slack_linked"  # a Slack user linked by signing in to canopy
APPROVAL = "approval"          # a person approved an item whose work this is
INTERNAL = "internal"          # canopy started it; see `accountable` for the human
# Contacts carry their own grade from `Contact.AUTH_*` (dmarc, dkim, spf, none,
# app_secret, app_signed, app_signed_origin) — the same ladder, not a copy.


@dataclass(frozen=True)
class Initiator:
    """One turn's asker. Construct with the helpers below, not directly."""

    kind: str
    via: str
    assurance: str
    user: Any = None       # the canopy user (kind=user), or the ACCOUNTABLE one (kind=system)
    contact: Any = None    # kind=contact
    agent_slug: str = ""   # kind=agent

    def fields(self) -> dict:
        """The `Turn` columns this initiator writes."""
        return {
            "initiator_kind": self.kind,
            "initiator_via": self.via[:64],
            "initiator_assurance": self.assurance[:32],
            "initiator_user": self.user if getattr(self.user, "pk", None) else None,
            "initiator_contact": self.contact if getattr(self.contact, "pk", None) else None,
            "initiator_agent": self.agent_slug[:64],
        }


def for_user(user, *, via: str, assurance: str) -> Initiator:
    if not getattr(user, "is_authenticated", False):
        return unknown(via=via)
    return Initiator(USER, via, assurance, user=user)


def for_contact(contact, *, via: str) -> Initiator:
    if contact is None:
        return unknown(via=via)
    # The contact's OWN grade — what its mail server or its host could prove —
    # rather than a flat "contact". An spf-only emailer and a host-signed widget
    # visitor are both contacts and are not the same claim.
    grade = getattr(contact, "auth_result", "") or "none"
    return Initiator(CONTACT, via, grade, contact=contact)


def system(*, via: str, accountable=None) -> Initiator:
    """canopy started it. `accountable` is the person behind it, where there is
    one — a schedule's creator — so a later phase can bound the turn by them."""
    user = accountable if getattr(accountable, "is_authenticated", False) else None
    return Initiator(SYSTEM, via, INTERNAL, user=user)


def for_agent(agent_slug: str, *, via: str) -> Initiator:
    return Initiator(AGENT, via, INTERNAL, agent_slug=agent_slug)


def unknown(*, via: str) -> Initiator:
    return Initiator(UNKNOWN, via, "")


def for_request(request, *, via: str) -> Initiator:
    """The asker behind an HTTP request, graded by how it authenticated.

    `request.auth_method` is stamped by `BearerTokenAuthMiddleware`; a request it
    did not touch was authenticated by canopy's own session.
    """
    contact = getattr(request, "contact", None)
    if contact is not None:
        return for_contact(contact, via=via)
    user = getattr(request, "user", None)
    method = getattr(request, "auth_method", "") or SESSION
    return for_user(user, via=via, assurance=method)


def for_scope(scope, *, via: str) -> Initiator:
    """The same question for a WebSocket scope (`RealtimeAuthMiddleware`)."""
    contact = scope.get("contact")
    if contact is not None:
        return for_contact(contact, via=via)
    return for_user(scope.get("user"), via=via,
                    assurance=scope.get("auth_method") or SESSION)


def channel(request, default: str) -> str:
    """The channel a request came through. An app-delegated or contact token
    means an embedding host's widget, and names the host — "chat" would hide
    that the person was on connect-labs rather than in canopy."""
    app = getattr(request, "delegated_app", None)
    return f"widget:{app.name}" if app is not None else default


def describe(turn) -> dict:
    """The initiator as the API and the runner see it. Names, not ids alone:
    the agent is going to read this and address a person."""
    user = getattr(turn, "initiator_user", None)
    contact = getattr(turn, "initiator_contact", None)
    out: dict = {
        "kind": turn.initiator_kind or UNKNOWN,
        "via": turn.initiator_via,
        "assurance": turn.initiator_assurance,
    }
    if user is not None:
        out["user"] = {
            "id": user.pk,
            "email": user.email,
            "name": user.get_full_name() or user.email,
        }
    if contact is not None:
        out["contact"] = {
            "id": contact.pk,
            "email": getattr(contact, "email", "") or "",
            "name": getattr(contact, "display_name", "") or getattr(contact, "email", ""),
        }
    if turn.initiator_agent:
        out["agent"] = turn.initiator_agent
    return out
