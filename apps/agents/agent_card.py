"""An agent's A2A Agent Card, GENERATED from its declared interface.

The Agent2Agent protocol (A2A v1.0, Linux Foundation) discovers an agent by
fetching a JSON "Agent Card": who it is, what skills it offers, where to reach
it and how to authenticate. canopy already holds every one of those facts —
the skills are the agent's declared capabilities (`apps/agents/interface.py`),
the auth is canopy's own — so the card is a RENDERING of that state, never a
second place policy lives. Nothing here decides who may do anything:
`interface.capability_for` does that at enqueue, on every turn, whatever a
card said. A stale or hand-copied card can therefore mislead a client, but it
cannot let one in.

**Where the cards live.** A2A's well-known URI is one card per ORIGIN
(`https://{domain}/.well-known/agent-card.json`), and canopy is neither one
agent nor the owner of its origin (it is served under `/canopy` on a domain
shared with connect-labs). So each agent gets its own base URL,
`{CANOPY_PUBLIC_BASE_URL}/api/a2a/agents/{slug}`, with the well-known path
under it — exactly how the reference SDK's card resolver composes a card URL
from an agent's base (`base_url + "/.well-known/agent-card.json"`). The
authenticated extended card sits beside it at `{base}/extendedAgentCard`, the
path the HTTP+JSON binding gives `GetExtendedAgentCard`.

**Who is discoverable.** An agent has a PUBLIC card only if it offers a
capability to someone outside its workspace — a caller class of `contact` or
`unknown`. Everything else 404s, identically to a slug that does not exist,
so a private agent's existence does not leak. That includes an agent whose
only outsider access is a `full:` rule: `full:` grants a whole working
profile, which is not a skill and is not advertised. The public card lists
only those outsider capabilities; the EXTENDED card adds what the requesting
canopy user may invoke themselves (`interface.offered_to`, the same answer the
MCP tool list gives them).

**What never appears.** A capability's `tools`, `bash`, `read_paths`,
`entry`, `pages` and `input` — the enforcement detail of its profile — and any
email address: a caller class names at most a DOMAIN (`contact@dimagi.com`),
which becomes a `domain:` tag. Neither card names the agent's workspace.

**Unsigned, for now.** A2A cards MAY carry JWS signatures (§8.4, RFC 7515 over
the RFC 8785 canonical form). canopy has no key suited to it yet — the
on-behalf-of key signs caller assertions for ONE host, and reusing it would
let a card and an assertion be confused for each other. FOLLOW-UP: sign with
the canopy client key being added under `/oauth/jwks.json`, as a
`signatures[]` entry whose protected header carries `kid` + `jku`.
"""
from __future__ import annotations

import hashlib
import json

from django.conf import settings

from . import interface as iface_mod
from .schemas import (
    A2AAgentCapabilities,
    A2AAgentCardOut,
    A2AAgentInterface,
    A2AAgentProvider,
    A2AAgentSkill,
    A2AAPIKeySecurityScheme,
    A2AHTTPAuthSecurityScheme,
    A2ASecurityRequirement,
    A2ASecurityScheme,
    A2AStringList,
)

#: The A2A protocol version these cards are written against.
A2A_PROTOCOL_VERSION = "1.0"

#: Caller classes that are not a member of the agent's workspace.
OUTSIDER_BASES = frozenset({"contact", "unknown"})

# Security scheme names, as keys of `securitySchemes`.
PAT = "canopyPersonalAccessToken"
SESSION = "canopySession"
CONTACT = "canopyContactToken"

# `protocolBinding` for the doors canopy actually has. None of them is one of
# A2A's own three bindings (JSONRPC / GRPC / HTTP+JSON) — canopy does not speak
# A2A messaging yet — so each is named by a URI, as spec §5.8 asks of a custom
# binding. A client that only speaks the standard bindings will see no binding
# it can use, which is the honest answer today.
#
# MCP: members call a capability as the tool `<slug>__<capability>` on
# canopy's MCP server (apps/mcp/agent_tools.py). Unversioned because MCP
# negotiates its own protocol version at `initialize`.
MCP_BINDING = "https://modelcontextprotocol.io/specification/latest/basic/transports#streamable-http"
# A contact holding a contact token opens a conversation at
# `POST /api/contact/sessions` and sends into it (apps/tokens/contact_api.py).
CONTACT_BINDING = "urn:dimagi:canopy:binding:contact-sessions:v1"
# Mail to the agent's own mailbox is a turn like any other (apps/inbound).
EMAIL_BINDING = "urn:dimagi:canopy:binding:email:v1"

TEXT = "text/plain"
JSON_MODE = "application/json"


def base_url() -> str:
    return (getattr(settings, "CANOPY_PUBLIC_BASE_URL", "") or "").rstrip("/")


def card_base(slug: str) -> str:
    """The agent's A2A base URL; its card is `{this}/.well-known/agent-card.json`."""
    return f"{base_url()}/api/a2a/agents/{slug}"


# --- which capabilities appear -----------------------------------------------------

def _parsed(klass: str) -> tuple[str, str, bool] | None:
    m = iface_mod._CALLER.match(klass or "")
    if not m:
        return None
    return m.group("base"), m.group("domain") or "", bool(m.group("verified"))


def _capabilities(agent) -> dict:
    iface = getattr(agent, "interface", None) or {}
    if not iface_mod.published(iface):
        return {}
    return iface.get("capabilities") or {}


def public_capabilities(agent) -> list[str]:
    """Capabilities someone outside the agent's workspace could invoke."""
    out = []
    for name, cap in _capabilities(agent).items():
        classes = [_parsed(c) for c in cap.get("callers") or []]
        if any(p and p[0] in OUTSIDER_BASES for p in classes):
            out.append(name)
    return sorted(out)


def _contact_token_reaches(cap: dict) -> bool:
    """Whether a visitor arriving by contact token can pass this capability.

    `:verified` is mail-only for a contact (interface.py), so a capability
    gated on `contact:verified` alone is reachable by email and nothing else.
    """
    for c in cap.get("callers") or []:
        p = _parsed(c)
        if p and p[0] == "contact" and not p[2]:
            return True
    return False


# --- rendering ---------------------------------------------------------------------

def _tags(cap: dict, *, outsiders_only: bool) -> list[str]:
    tags: set[str] = set()
    for c in cap.get("callers") or []:
        p = _parsed(c)
        if p is None:
            continue
        base, domain, verified = p
        if outsiders_only and base not in OUTSIDER_BASES:
            continue
        tags.add(f"caller:{base}")
        if domain:
            # A DOMAIN, never an address: caller classes cannot name a person.
            tags.add(f"domain:{domain}")
        if verified:
            tags.add("verified-sender")
    # `tags` is REQUIRED and must be non-empty; a capability no class is
    # listed for is one only the agent's owner and admins reach.
    return sorted(tags) or ["caller:admin"]


def _requirement(*names: str) -> A2ASecurityRequirement:
    return A2ASecurityRequirement(schemes={n: A2AStringList(values=[]) for n in names})


def _skill(agent, name: str, cap: dict, *, public: bool, invokable_by_user: bool) -> A2AAgentSkill:
    security = []
    if invokable_by_user:
        security.append(_requirement(PAT))
    if _contact_token_reaches(cap):
        security.append(_requirement(CONTACT))
    return A2AAgentSkill(
        id=name,
        name=name.replace("_", " ").capitalize(),
        description=(cap.get("description") or "").strip() or f"Ask {agent.name} ({name}).",
        tags=_tags(cap, outsiders_only=public),
        # A capability with typed `input` fields takes them as JSON alongside
        # the message (they are parameters of its MCP tool); their names and
        # types are on that tool, not repeated here.
        input_modes=[TEXT, JSON_MODE] if cap.get("input") else None,
        security_requirements=security or None,
    )


def _security_schemes() -> dict[str, A2ASecurityScheme]:
    base = base_url()
    return {
        PAT: A2ASecurityScheme(http_auth_security_scheme=A2AHTTPAuthSecurityScheme(
            scheme="bearer",
            bearer_format="canopy personal access token",
            description=(
                "A canopy Personal Access Token, sent as `Authorization: Bearer <token>`. "
                "Minted by a signed-in member of the agent's workspace (Google sign-in, "
                f"restricted to the deployment's allowed domains) at {base}/settings. "
                f"Accepted by the MCP server at {base}/api/mcp/ and by this card's "
                "extendedAgentCard endpoint."),
        )),
        SESSION: A2ASecurityScheme(api_key_security_scheme=A2AAPIKeySecurityScheme(
            location="cookie",
            name=getattr(settings, "SESSION_COOKIE_NAME", "sessionid"),
            description=(
                "canopy's browser session, established by signing in with Google at "
                f"{base}/accounts/login/. Accepted by the extendedAgentCard endpoint."),
        )),
        CONTACT: A2ASecurityScheme(http_auth_security_scheme=A2AHTTPAuthSecurityScheme(
            scheme="bearer",
            bearer_format="canopy contact token",
            description=(
                "A short-lived contact token for someone with no canopy account, sent as "
                "`Authorization: Bearer <token>`. Issued at "
                f"POST {base}/api/auth/contact-token in exchange for a signed assertion "
                "(an asymmetrically signed JWT) from a site the agent's workspace has "
                "connected and allowed to offer this agent. Valid only under "
                f"{base}/api/contact/."),
        )),
    }


def _card(agent, skills: list[A2AAgentSkill], interfaces: list[A2AAgentInterface]) -> A2AAgentCardOut:
    published_at = getattr(agent, "interface_published_at", None)
    return A2AAgentCardOut(
        name=agent.name,
        description=(agent.description or "").strip() or f"{agent.name}, an agent hosted on canopy.",
        supported_interfaces=interfaces,
        provider=A2AAgentProvider(
            organization=getattr(settings, "CANOPY_A2A_PROVIDER_ORGANIZATION", "Dimagi"),
            url=getattr(settings, "CANOPY_A2A_PROVIDER_URL", "https://www.dimagi.com"),
        ),
        # Moves whenever the interface is republished, so a client caching by
        # version sees a change of skills.
        version=f"1.0.{int(published_at.timestamp())}" if published_at else "1.0.0",
        documentation_url=f"{base_url()}/api/docs/",
        capabilities=A2AAgentCapabilities(streaming=False, push_notifications=False,
                                          extended_agent_card=True),
        security_schemes=_security_schemes(),
        # Alternatives (any ONE suffices), each a door some skill uses. The
        # session cookie is declared as a scheme but not listed: it fetches the
        # extended card, and invokes nothing.
        security_requirements=[_requirement(CONTACT), _requirement(PAT)],
        default_input_modes=[TEXT],
        default_output_modes=[TEXT],
        skills=skills,
        icon_url=(agent.avatar_url or None),
    )


def _interfaces(agent, caps: dict, public_names: list[str], *, member_first: bool) -> list[A2AAgentInterface]:
    base = base_url()
    mcp = A2AAgentInterface(url=f"{base}/api/mcp/", protocol_binding=MCP_BINDING,
                            protocol_version=A2A_PROTOCOL_VERSION)
    outsider = []
    if any(_contact_token_reaches(caps[n]) for n in public_names):
        outsider.append(A2AAgentInterface(url=f"{base}/api/contact/sessions",
                                          protocol_binding=CONTACT_BINDING,
                                          protocol_version=A2A_PROTOCOL_VERSION))
    if public_names and agent.email:
        # The agent's own mailbox — an address it chose to open to outsiders
        # by publishing a capability for them. Never a person's.
        outsider.append(A2AAgentInterface(url=f"mailto:{agent.email}",
                                          protocol_binding=EMAIL_BINDING,
                                          protocol_version=A2A_PROTOCOL_VERSION))
    return [mcp, *outsider] if member_first else [*outsider, mcp]


def public_card(agent) -> A2AAgentCardOut | None:
    """The card anyone may read, or None when the agent offers outsiders nothing."""
    names = public_capabilities(agent)
    if not names:
        return None
    caps = _capabilities(agent)
    skills = [_skill(agent, n, caps[n], public=True, invokable_by_user=False) for n in names]
    return _card(agent, skills, _interfaces(agent, caps, names, member_first=False))


def extended_card(agent, user) -> A2AAgentCardOut | None:
    """The card for a signed-in canopy user: the public skills plus the ones
    `user` may invoke. None when that is nothing — the same answer, to them, as
    an agent that does not exist."""
    caps = _capabilities(agent)
    if not caps:
        return None
    mine = set(iface_mod.offered_to(user, agent))
    public_names = public_capabilities(agent)
    names = sorted(mine | set(public_names))
    if not names:
        return None
    skills = [_skill(agent, n, caps[n], public=n not in mine, invokable_by_user=n in mine)
              for n in names]
    return _card(agent, skills, _interfaces(agent, caps, public_names, member_first=bool(mine)))


def dump(card: A2AAgentCardOut) -> dict:
    """The card's JSON form: camelCase, unset optional fields omitted."""
    return card.model_dump(by_alias=True, exclude_none=True, mode="json")


def etag(card: A2AAgentCardOut) -> str:
    body = json.dumps(dump(card), sort_keys=True, separators=(",", ":"))
    return '"' + hashlib.sha256(body.encode()).hexdigest()[:32] + '"'
