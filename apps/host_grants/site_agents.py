"""The site agents API: what canopy tells a connected site, signed, about the
agents it may show there (spec connect-labs `2026-10-22-canopy-site-agents-api-design` §1).

A site used to keep hand-typed copies of canopy's facts — which agents, which
handle sensitive data, which runner flags exist — and a wrong copy showed only as an
agent that silently did not appear. `GET /api/sites/agents` replaces the copies:
the site asks with a JWT signed by its own key (the one canopy already verifies
its visitor assertions with), and canopy answers with a JWS signed by its
client key (the `Canopy-Execution` key, `kid` in `/oauth/client.json`). Both
anchors existed before this; nothing new is shared.

`site_agents_content(app)` is the ONE answer to "which agents may this site show, and on
what terms" — the endpoint signs it, and the Connected sites page reads the same
function to say whether each agent meets what the site asks
(`site_needs.fit`). The wire format and its checks are the SDK's
(`canopy_sdk.site_agents`), which every site verifies with.

What each agent entry says, and where it comes from:

* listed: the site's tenant allows it (`AppCredentialAgent`) — allowed but
  with no capability naming the site in `sites:` is listed `ready: false`,
  with a problem saying so;
* `capabilities`: those that name the site; `handles_sensitive` is `data:
  sensitive`, and who/when is who last published the interface holding it — exactly
  what `Canopy-Execution`'s `declaration` states (`sensitive_data.as_claim`), so this API and
  the per-call statement cannot disagree;
* `runners`: the boxes that would take its turns (`setup.runner_takers`, the
  checklist's own definition); `flags_available` is what any of them carries in
  force, `ready_with[flag]` whether one carrying it can take work now;
* `ready`: a capability for the site AND a runner ready now. A GitHub token is
  NOT a condition: a site visitor's turn is confined to a capability and is
  never lent one (`delegations.github_token_for_turn`).
"""
from __future__ import annotations

from canopy_sdk import contract
from canopy_sdk import site_agents as wire
from canopy_sdk.jose import unverified_claims
from django.conf import settings
from django.core.cache import cache

from apps.agents import sensitive_data
from apps.agents.models import Agent, AgentBootstrapReport
from apps.agents.setup import runner_takers
from apps.embedding import assertions
from apps.embedding.embed_apps import AmbiguousSite, SiteTenantMismatch, resolve_site
from apps.harness.models import Runner
from apps.harness.routing import unit_refusal
from apps.harness.runner_requirements import MEANINGS, SENSITIVE_DATA, in_force
from apps.workspaces.links import scoped_url

from . import client_identity


class SiteAgentsRefused(Exception):
    """The request is not one canopy answers. `code` is stable; `status` the HTTP one."""

    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def audiences() -> list[str]:
    """What a request may be addressed to: the endpoint URL under canopy's protocol
    identity, and under the address people visit (the same deployment, so a site
    that wrote either is talking to us)."""
    bases = [client_identity.public_base(),
             (getattr(settings, "CANOPY_PUBLIC_BASE_URL", "") or "").rstrip("/")]
    return list(dict.fromkeys(wire.endpoint_url(b) for b in bases if b))


def site_for_request(token: str):
    """The site a request CLAIMS to be from — named, not yet trusted. Resolved as
    every site name is (`embed_apps.resolve_site`), with the workspace the
    request names (`canopy_workspace`) — or, without one, the agent it names —
    telling apart tenants sharing a name."""
    try:
        claims = unverified_claims(token)
    except contract.ContractError as exc:
        raise SiteAgentsRefused("malformed", exc.message, 400) from exc
    name = str(claims.get("iss") or "").strip()
    try:
        agent = contract.parse_agent_claim(claims.get(contract.AGENT_CLAIM))
        workspace = contract.parse_workspace_claim(claims.get(contract.WORKSPACE_CLAIM))
    except ValueError as exc:
        raise SiteAgentsRefused("malformed", str(exc), 400) from exc
    try:
        app = resolve_site(name, agent, workspace)
    except AmbiguousSite as exc:
        raise SiteAgentsRefused("ambiguous_issuer", str(exc), 409) from exc
    except SiteTenantMismatch as exc:
        raise SiteAgentsRefused(exc.code, str(exc), 409) from exc
    if app is None:
        # One wording for unknown, revoked and another tenant's: a prober learns
        # nothing about which names are real.
        raise SiteAgentsRefused("unknown_issuer", f"no connected site named {name!r}")
    return app


def verify_request(token: str):
    """`(app, claims)` for a request signed by a registered site's own key, or
    `SiteAgentsRefused`. The keys are the ones its visitor assertions verify with
    (`assertions.keys_for_app`: pasted PEMs plus its published JWKS)."""
    app = site_for_request(token)
    try:
        keys = assertions.keys_for_app(app, token)
    except assertions.AssertionError_ as exc:
        raise SiteAgentsRefused(exc.code, exc.message) from exc

    def spend(claims: dict) -> None:
        ttl = max(1, int(claims["exp"]) - int(claims["iat"]) + contract.LEEWAY_SECONDS)
        # Fails CLOSED: a cache that cannot record the jti reads as a replay.
        if not cache.add(f"site-agents:jti:{app.pk}:{claims['jti']}", 1, timeout=ttl):
            raise wire.SiteAgentsError("replayed", "this request has already been used")

    try:
        claims = wire.verify_request(token, keys, audiences=audiences(), spend_jti=spend,
                                     algorithms=assertions.ALLOWED_ALGORITHMS)
    except wire.SiteAgentsError as exc:
        raise SiteAgentsRefused(exc.code, exc.message, 400 if exc.code in ("malformed", "bad_alg") else 401) from exc
    if claims.get("iss") != app.name:
        raise SiteAgentsRefused("unknown_issuer", f"no connected site named {claims.get('iss')!r}")
    return app, claims


# --- the content --------------------------------------------------------------------


def runner_flags() -> list[dict]:
    """Every flag canopy knows (`contract.RUNNER_FLAGS`), with what it means."""
    return [{"flag": f, "meaning": MEANINGS.get(f, "")} for f in sorted(contract.RUNNER_FLAGS)]


def _person(user) -> str:
    if user is None:
        return ""
    return (user.get_full_name() or "").strip() or user.email or ""


def site_capabilities(agent: Agent, site: str) -> list[dict]:
    """The agent's published capabilities that name `site` in `sites:`, as listed."""
    out = []
    caps = ((agent.interface or {}).get("capabilities") or {})
    for name in sorted(caps):
        cap = caps[name] or {}
        if site not in (cap.get("sites") or []):
            continue
        claim = sensitive_data.as_claim(agent, name)
        out.append({
            "name": name,
            "description": cap.get("description") or "",
            "callers": list(cap.get("callers") or []),
            "handles_sensitive": claim is not None,
            "sensitive_set_by": _person(agent.interface_published_by) if claim else "",
            "sensitive_set_at": (agent.interface_published_at.isoformat()
                           if claim and agent.interface_published_at else ""),
        })
    return out


def _runners(agent: Agent) -> tuple[list[Runner], list[Runner]]:
    """`(takers, ready)`: the boxes that would take its turns, and those of them
    that could take one now. A cloud box serves an agent only once its bootstrap
    has reported for it (the setup checklist's rule)."""
    takers = [r for r in runner_takers(agent)[0] if not unit_refusal(r, agent.slug)]
    reported = set(AgentBootstrapReport.objects.filter(agent=agent)
                   .values_list("runner_name", flat=True))
    ready = [r for r in takers if r.is_available
             and (r.kind != Runner.CLOUD or r.name in reported)]
    return takers, ready


def agent_entry(app, agent: Agent) -> dict:
    capabilities = site_capabilities(agent, app.name)
    takers, ready_runners = _runners(agent)
    available = sorted(set().union(*(in_force(r) for r in takers))) if takers else []
    ready_with = {f: any(f in in_force(r) for r in ready_runners) for f in sorted(contract.RUNNER_FLAGS)}
    settings_url = scoped_url(agent.workspace_id, f"/agents/{agent.slug}/settings")
    reach, running = f"{settings_url}#reach", f"{settings_url}#running"

    # `(sentence, where it is fixed)`. The sentence keeps naming the place too:
    # `problems` is what a host that predates `problem_links` shows.
    found: list[tuple[str, str]] = []
    if not capabilities:
        found.append((f"{agent.name} has no capability naming {app.name} in `sites:` — publish "
                      f"one on its interface: canopy › {reach}", reach))
    if not takers:
        found.append((f"no runner takes {agent.name}'s turns: canopy › {running}", running))
    elif not ready_runners:
        names = ", ".join(sorted(r.name for r in takers))
        found.append((f"no runner that takes {agent.name}'s turns is ready now ({names}): "
                      f"canopy › {running}", running))
    if any(c["handles_sensitive"] for c in capabilities) and not ready_with.get(SENSITIVE_DATA):
        found.append((f"no ready runner with {SENSITIVE_DATA} for {agent.name}: "
                      f"canopy › {running}", running))
    return {
        "slug": agent.slug,
        "name": agent.name,
        "description": agent.description or "",
        "workspace": agent.workspace_id,
        "owner": _person(agent.owner),
        "url": scoped_url(agent.workspace_id, f"/agents/{agent.slug}"),
        "capabilities": capabilities,
        "runners": {"flags_available": available, "ready_with": ready_with},
        "ready": bool(capabilities) and bool(ready_runners),
        "problems": [text for text, _url in found],
        "problem_links": [{"text": text, "url": url} for text, url in found],
    }


def listed_agents(app) -> list[Agent]:
    """The agents the site's tenant allows on it — its own workspace's only
    (`embed_apps.set_agents` keeps it so; this holds it on read too)."""
    return list(Agent.objects
                .filter(embedding_apps__app=app, workspace_id=app.workspace_id)
                .select_related("owner", "interface_published_by")
                .order_by("slug"))


def site_settings_url(app) -> str:
    """This connected site's own place in canopy's UI: its card on the
    workspace's Connected sites page, where its owner allows agents, changes its
    keys and URLs, and tests it."""
    return scoped_url(app.workspace_id, f"/settings/connected-apps#site-{app.pk}")


def site_agents_content(app) -> dict:
    """`site`, `runner_flags` and `agents` — everything `version` covers."""
    return {
        "site": {"name": app.name, "tenant": app.workspace_id,
                 "settings_url": site_settings_url(app)},
        "runner_flags": runner_flags(),
        "agents": [agent_entry(app, a) for a in listed_agents(app)],
    }


def signed_site_agents(app) -> str:
    """The compact JWS canopy answers with. Raises `ClientIdentityError` when
    this deployment has no client key."""
    return client_identity.sign_site_agents(app.name, site_agents_content(app))
