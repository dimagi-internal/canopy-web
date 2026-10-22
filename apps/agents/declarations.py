"""An author's PHI-safe declaration: which capabilities of an agent may serve a
host's sensitive data, and at which commit (spec 2026-10-22-phi-agents-on-zdr-runners
§5).

A capability says `data: phi` on the published interface. That is accepted only

* **when it is structurally true** (`structural_refusals`, asked by
  `interface.parse`): a phi capability reaches its host through canopy's gateway
  (`sites:` is required), and its tools may be only the gateway's site tools,
  `current_page`, `who_is_asking` and read-only local tools — no shell, no
  writes, nothing that leaves the box (Drive, email, memory, Slack, the web).
  Anything else is a 422 naming it;
* **from a person**, in a browser, behind a passkey step-up, naming the agent
  repo commit it covers (`publish_interface`, `record`).

What it buys, and what reads it:

* **confinement for everyone** — a turn in a phi capability, or any turn in a
  `zdr` conversation, runs the capability's confined profile whoever asked
  (`access.decide`, `phi_capability`);
* **the declared commit runs** — at claim the turn is stamped with the current
  declaration's SHA (`Turn.agent_sha`), and the runner checks out that commit;
* **canopy vouches for it to the host** — the gateway's `Canopy-Execution`
  statement carries the declaration, and the host decides whether to accept it.
"""
from __future__ import annotations

import re

import requests
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.common.encryption import decrypt_secret
from apps.events import services as events
from apps.harness.models import Turn

from . import interface as iface_mod
from .credentials import delegations
from .models import AgentDeclaration

PHI = "phi"
DATA_VALUES = frozenset({PHI})

#: Local tools a phi capability may hold: they read the agent's own checkout.
READ_ONLY_LOCAL = frozenset({"Read", "Grep", "Glob", "Skill", "ToolSearch", "TodoWrite"})
#: canopy's own tools a phi capability may hold (any MCP server prefix ending in
#: `canopy-web__`, as interface tool patterns are written).
_CANOPY_TOOL = re.compile(
    r"^mcp__(?:\*|[A-Za-z0-9_.-]*?)canopy-web__(?:current_page|who_is_asking|site_tools|site_call)$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


def structural_refusals(name: str, cap: dict) -> list[str]:
    """Why `cap` cannot honestly be declared PHI-safe — each named. Empty: it can."""
    out = []
    if not cap.get("sites"):
        out.append(f"{name}: a `data: phi` capability must name `sites:` — a host's data "
                   "reaches it only through canopy's gateway")
    bad = [t for t in cap.get("tools") or []
           if t not in READ_ONLY_LOCAL and not _CANOPY_TOOL.match(t)]
    if bad:
        out.append(f"{name}: a `data: phi` capability may hold only the gateway's site tools, "
                   f"current_page, who_is_asking and {', '.join(sorted(READ_ONLY_LOCAL))}; "
                   f"refused: {', '.join(bad)}")
    if cap.get("bash"):
        out.append(f"{name}: a `data: phi` capability may run no shell (`bash:` must be empty)")
    if cap.get("write_paths"):
        out.append(f"{name}: a `data: phi` capability may write nothing (`write_paths:` must "
                   "be empty)")
    return out


def phi_capabilities(iface: dict | None) -> list[str]:
    return sorted(n for n, c in ((iface or {}).get("capabilities") or {}).items()
                  if (c or {}).get("data") == PHI)


def is_phi(agent, capability: str | None) -> bool:
    if not capability:
        return False
    cap = ((getattr(agent, "interface", None) or {}).get("capabilities") or {}).get(capability)
    return bool(cap) and cap.get("data") == PHI


def valid_sha(sha: str | None) -> bool:
    return bool(sha) and bool(_SHA.match(sha))


def current(agent, capability: str):
    """The declaration in force for one capability, or None."""
    if not capability:
        return None
    return (AgentDeclaration.objects.filter(agent=agent, capability=capability,
                                            withdrawn_at__isnull=True)
            .select_related("declared_by").order_by("-declared_at", "-pk").first())


def covers(agent, capability: str, sha: str) -> bool:
    """Whether the current declaration of `capability` covers commit `sha`."""
    decl = current(agent, capability)
    return decl is not None and bool(sha) and decl.sha == sha


def commit_url(agent, sha: str) -> str:
    """The commit on GitHub, for a person to read what they vouch for; "" when
    the agent's repo is not a github.com one."""
    repo = delegations.agent_repo(agent)
    return f"https://github.com/{repo}/commit/{sha}" if repo and sha else ""


def last_run_sha(agent, capability: str):
    """(sha, claimed_at) of the latest turn claimed in `capability`, or (None, None).

    The commit a phi turn ran is the one stamped at claim (`Turn.agent_sha`), and
    the gateway gives a host's data only to a turn whose stamp the current
    declaration covers — so a stamp that differs is a turn hosts were refused to.
    """
    turn = (Turn.objects.filter(Q(agent=agent) | Q(chat_session__agent=agent),
                                capability=capability, claimed_at__isnull=False)
            .only("agent_sha", "claimed_at").order_by("-claimed_at").first())
    return (turn.agent_sha, turn.claimed_at) if turn is not None else (None, None)


def repo_head(agent) -> dict:
    """The head of the agent repo's `repo_ref`, as GitHub reports it to the
    agent's own GitHub identity (`delegations.delegation_for`) — the commit an
    author is offered to declare. `sha` is "" with a `detail` saying why when
    canopy cannot tell; the person then types the commit."""
    repo, ref = delegations.agent_repo(agent), (agent.repo_ref or "").strip() or "main"
    out = {"repo": repo, "ref": ref, "sha": "", "commit_url": "", "detail": ""}
    if not repo:
        out["detail"] = "the agent has no github.com repository set"
        return out
    row = delegations.delegation_for(agent)
    if row is None:
        out["detail"] = "the agent has no GitHub identity to read its repository with"
        return out
    try:
        resp = requests.get(f"{delegations.GITHUB_API}/repos/{repo}/commits/{ref}",
                            headers=delegations.github_headers(decrypt_secret(row.secret_enc)),
                            timeout=delegations.HTTP_TIMEOUT)
    except requests.RequestException:
        out["detail"] = "GitHub did not answer"
        return out
    sha = (resp.json() or {}).get("sha", "") if resp.ok else ""
    if not valid_sha(sha):
        out["detail"] = f"GitHub would not say what {repo}@{ref} is ({resp.status_code})"
        return out
    out.update(sha=sha, commit_url=commit_url(agent, sha))
    return out


def as_claim(decl) -> dict | None:
    """The declaration as canopy states it to a host (`Canopy-Execution`)."""
    if decl is None:
        return None
    by = decl.declared_by
    return {"data": decl.data, "sha": decl.sha, "capability": decl.capability,
            "declared_by": getattr(by, "email", "") if by is not None else "",
            "declared_at": decl.declared_at.isoformat() if decl.declared_at else ""}


def record(agent, *, sha: str | None, by) -> dict[str, list[str]]:
    """After a publish: declare each phi capability at `sha`, withdraw any
    capability that no longer is (or is gone). Returns {"declared", "withdrawn"}.

    A phi capability is re-declared on EVERY publish that holds it, even
    unchanged: the publish is a person's act behind a passkey, and the commit
    it names is the one that will run.
    """
    now = timezone.now()
    wanted = phi_capabilities(agent.interface)
    declared, withdrawn = [], []
    with transaction.atomic():
        live = AgentDeclaration.objects.select_for_update().filter(agent=agent,
                                                                   withdrawn_at__isnull=True)
        for decl in live:
            decl.withdrawn_at = now
            decl.save(update_fields=["withdrawn_at"])
            if decl.capability not in wanted:
                withdrawn.append(decl.capability)
        for name in wanted:
            AgentDeclaration.objects.create(agent=agent, capability=name, data=PHI, sha=sha or "",
                                            profile=iface_mod.profile(agent, name) or {},
                                            declared_by=by)
            declared.append(name)
    email = getattr(by, "email", "") or "someone"
    rows = [{"source": "agents.declarations", "kind": "agent.phi_declared", "level": "info",
             "summary": f"{email} declared {agent.slug}'s '{n}' PHI-safe at {(sha or '')[:12]}",
             "payload": {"agent": agent.slug, "capability": n, "sha": sha, "by": email}}
            for n in declared]
    rows += [{"source": "agents.declarations", "kind": "agent.phi_withdrawn", "level": "info",
              "summary": f"{agent.slug}'s '{n}' is no longer declared PHI-safe",
              "payload": {"agent": agent.slug, "capability": n, "by": email}}
             for n in sorted(set(withdrawn))]
    if rows:
        events.record(rows, workspace=agent.workspace)
    return {"declared": declared, "withdrawn": sorted(set(withdrawn))}

