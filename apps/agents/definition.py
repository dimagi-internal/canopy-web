"""Which agents share a definition — and what a "definition" even is here.

**THE DEFINITION IS THE REPO, NOT A DATABASE ROW.** An `Agent` row is one
tenant's running INSTANCE: its board, its credentials, its turns, its runner
assignments, its mailbox. What the agent *is* — persona, skills, config — lives
in the git repository the row points at (`repo_url` @ `repo_ref`).

This replaced a designed-but-unbuilt `AgentInstance` model, and the codebase
settled the argument itself: `AgentSkill`'s docstring says the catalog "is
replaced wholesale on each publish (PUT) so it always mirrors the repo". The
old design named `AgentSkill` as the ONE model that would live on the
definition, and used that as its sanity check — "if more than one or two models
want to live on the definition, the boundary is drawn wrong". It turns out zero
do: the single candidate is a cache of the repo. So the definition was never a
database concept, and `Agent.workspace` being NOT NULL was already the instance
model.

What that buys, beyond not building a table: the migration would have made
tenancy nullable across ~70 call sites during the transition, reintroducing the
`workspace_id IS NULL` means *allow* surface that `Agent.workspace` was made NOT
NULL to eliminate. Nothing here becomes nullable.

Sharing still works, and works the way it already did: two instances pointing at
the same repo both pull the same skills, so improving echo improves it
everywhere without anyone copying files. Forking is pointing at a different repo
— or a different ref of the same one.

WHAT IS STILL TRUE AND UNBUILT: `Agent.slug` is globally unique, so two tenants
cannot both run an agent called `echo` yet. That is the one constraint gating
the payoff, and relaxing it is a `UniqueConstraint(["workspace", "slug"])` plus
teaching ~100 bare-slug lookups which tenant they mean. Deliberately deferred
until a second tenant actually wants the same agent: doing it speculatively
changes resolution for every flat-mount caller — the whole PAT/plugin fleet —
for no present benefit.
"""
from __future__ import annotations

import re

#: `git@github.com:org/repo.git` -> `github.com/org/repo`
_SCP_LIKE = re.compile(r"^[\w.+-]+@([^:]+):(.+)$")
_SCHEME = re.compile(r"^[a-zA-Z][\w+.-]*://")


def definition_key(repo_url: str, repo_ref: str = "") -> str:
    """A canonical identity for a repo, so two spellings of it compare equal.

    `https://github.com/dimagi-internal/ace`,
    `https://github.com/dimagi-internal/ace.git` and
    `git@github.com:dimagi-internal/ace.git` are the same definition, and an
    equality test on the raw column would say they are three. Since this is the
    only thing tying two instances together, a normalisation miss silently means
    "these agents are unrelated" — which is the failure that matters, because it
    is invisible.

    The REF is deliberately part of the key when set to something other than the
    default branch: an instance pinned to `staging` is not running the same
    definition as one on `main`, and treating them as identical would report a
    fix as having reached a tenant it has not reached.

    Returns "" for an agent with no repo — which is not an error, just an agent
    whose definition canopy cannot see. "" never matches "", so such agents are
    never reported as sharing a definition with each other.
    """
    url = (repo_url or "").strip()
    if not url:
        return ""

    m = _SCP_LIKE.match(url)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        if _SCHEME.match(url):
            url = url.split("://", 1)[1]
        # Strip any userinfo (`user:pass@host`) before splitting host from path.
        if "@" in url.split("/", 1)[0]:
            url = url.split("@", 1)[1]
        host, _, path = url.partition("/")

    host = host.lower().rstrip(":")
    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[: -len(".git")]
    key = f"{host}/{path}".lower().rstrip("/")

    ref = (repo_ref or "").strip()
    # `main`/`master`/blank all mean "the default", so they must not split a
    # definition in two.
    if ref and ref.lower() not in {"main", "master"}:
        key = f"{key}@{ref}"
    return key


def siblings(agent):
    """Other agent instances running the SAME definition, across all tenants.

    Cross-tenant on purpose, and the one place in this codebase that is: the
    question "who else runs echo, and would this fix reach them?" is inherently
    about the fleet rather than about one workspace. It returns instances, not
    their contents — no board, no credentials, no turns — so it discloses that
    a tenant runs a given repo and nothing about what it does with it.

    Returns an empty queryset for an agent with no repo rather than matching
    every other repoless agent, which is what a naive `repo_url=""` filter would
    do.
    """
    from .models import Agent

    key = definition_key(agent.repo_url, agent.repo_ref)
    if not key:
        return Agent.objects.none()
    same = [
        a.pk
        for a in Agent.objects.exclude(pk=agent.pk).only("id", "repo_url", "repo_ref")
        if definition_key(a.repo_url, a.repo_ref) == key
    ]
    return Agent.objects.filter(pk__in=same).select_related("workspace")
