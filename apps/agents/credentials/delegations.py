"""A person lending their GitHub identity to one agent: a credential delegated to it in its vault (`accounts`).

Request-free service layer: the agent routes (set / check / clear), the harness
runner route (issue one token to one turn) and the runner-readiness route all
call these, so they cannot drift about which delegation is in force.

THE ONE RULE: a turn's GitHub credential is the delegation in force for its
agent — the one a person who is still an admin of it lent (`accounts.serves`) — resolved here, server-side. The runner names a turn it has claimed and
nothing else — it never chooses whose identity to use, and there is no fallback
to any shared token when the delegation is missing. A turn with no agent (a repo
chat, a project turn) has no GitHub credential at all — unless its repo has a
`RepoIdentity` row, in which case it runs as that agent, under the same rule.

The caller of a turn (the person who emailed the agent, say) is deliberately NOT
the principal: an agent's GitHub writes use the identity of whoever lent it
(its owner, or an admin when the owner has not — Jonathan, 2026-10-21), whoever
caused them (Jonathan, 2026-09-24). Stopping an agent from doing something bad is the
agent's own design responsibility — its gating hooks and sender triage — not the
token layer's. Who asked is RECORDED instead (`requested_by`), so attribution
shows both the credential and the person who caused the change.
"""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlencode

import requests
from django.db import transaction
from django.utils import timezone


from ..definition import definition_key
from ..models import Agent, AgentCredential, RepoIdentity
from . import accounts, zk

GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT = 15
#: Warn this long before a token lapses — long enough to make a new one at
#: leisure, short enough not to nag from the day it is created.
EXPIRY_WARNING = dt.timedelta(days=14)
#: A branch that cannot exist, for the pull-request probe. GitHub checks the
#: token's permission BEFORE it validates the branch, so the answer to
#: `POST /repos/{r}/pulls` with this head is 422 (allowed — and nothing is
#: created) or 403/404 (not allowed). The one GitHub call that answers "may
#: this token open a pull request here" without opening one.
_PROBE_HEAD = "canopy-permission-probe/does-not-exist"
#: The push probe's twin: asking to create a branch at a commit that cannot
#: exist. GitHub checks Contents: write before it looks for the commit, so 422
#: = may push (and nothing is created), 403/404 = may not. Verified 2026-09-26:
#: 422 on a writable repo, 403 on a repo outside a fine-grained token's
#: selection, 404 on one the person cannot write at all.
_PROBE_SHA = "0" * 39 + "1"
_REPO_PATH = re.compile(r"^github\.com/([\w.-]+)/([\w.-]+)$")
#: Every GitHub token is letters, digits and underscores (`github_pat_…`,
#: `ghp_…`, `gho_…`). Anything else is a paste that caught more than the token,
#: and sending it as a header would fail inside `requests` (a curly quote is not
#: latin-1) instead of with a sentence the person can act on.
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_]+$")


class DelegationError(Exception):
    """The delegation cannot be used, and a person must fix it.

    The message is written to be shown as-is — to the owner on the settings
    screen, and to a runner's operator in a refused turn."""


# ---- identifying the agent's repo --------------------------------------------

def repo_full_name(repo_url: str) -> str:
    """`owner/repo` for a github.com URL in any spelling, else ""."""
    key = definition_key(repo_url or "").split("@", 1)[0]
    m = _REPO_PATH.match(key)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def agent_repo(agent: Agent) -> str:
    return repo_full_name(agent.repo_url)


def create_url(agent: Agent) -> str:
    """GitHub's new-token form, filled in for this agent.

    GitHub accepts name, description, resource owner, lifetime and permissions
    as query parameters (changelog 2025-08-26). It does NOT accept a repository
    selection, so the owner still picks the repos — the UI says which. And the
    resource owner it pre-selects has been reported to fall back to the
    person's own account on submit (community discussion #188111), which is why
    `set_github` refuses a token that cannot reach the agent's repo rather than
    trusting the form.
    """
    return token_form_url(agent.slug, agent_repo(agent))


def token_form_url(slug: str, repo: str) -> str:
    """`create_url` for an agent that may not exist yet — the create page asks for
    this token before there is an `Agent` row, and the same token becomes its
    delegation."""
    params = {
        "name": f"canopy {slug}"[:40],
        "description": f"canopy-web agent '{slug}' acting as you on GitHub",
        "expires_in": "366",
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "workflows": "write",
    }
    if repo:
        params["target_name"] = repo.split("/", 1)[0]
    return f"https://github.com/settings/personal-access-tokens/new?{urlencode(params)}"


# ---- talking to GitHub -------------------------------------------------------

def github_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse_expiry(header: str | None) -> dt.datetime | None:
    # e.g. "2026-10-22 19:54:19 UTC"; absent for a token that never expires.
    if not header:
        return None
    try:
        return dt.datetime.strptime(header.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=dt.UTC)
    except ValueError:
        return None


def inspect_github(token: str) -> dict:
    """Who a token acts as, and when it expires. Raises DelegationError when
    it is not shaped like a token or GitHub rejects it outright."""
    if not _TOKEN_SHAPE.match(token or ""):
        raise DelegationError(
            f"that paste is not just a GitHub token ({len(token or '')} characters, with spaces "
            "or punctuation) — copy only the token GitHub showed you, which starts with "
            "github_pat_ and is about 93 characters")
    try:
        resp = requests.get(f"{GITHUB_API}/user", headers=github_headers(token), timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise DelegationError(f"could not reach GitHub to check the token: {exc}") from exc
    if resp.status_code == 401:
        raise DelegationError("GitHub rejected this token (401) — it is wrong, expired or revoked")
    if resp.status_code != 200:
        raise DelegationError(f"GitHub answered {resp.status_code} for /user")
    body = resp.json()
    expires = _parse_expiry(resp.headers.get("github-authentication-token-expiration"))
    return {
        "login": body.get("login") or "",
        "user_id": int(body.get("id") or 0),
        "name": body.get("name") or body.get("login") or "",
        "expires_at": expires.isoformat() if expires else None,
    }


def probe_pull_request(token: str, repo: str) -> dict:
    """May this token open a pull request on `repo`? Creates nothing — see
    `_PROBE_HEAD`. Returns `{repo, ok, detail}`."""
    try:
        resp = requests.post(
            f"{GITHUB_API}/repos/{repo}/pulls",
            headers=github_headers(token),
            json={"title": "canopy permission probe", "head": _PROBE_HEAD, "base": "main"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        return {"repo": repo, "ok": False, "detail": f"could not reach GitHub: {exc}"}
    if resp.status_code == 422:
        return {"repo": repo, "ok": True, "detail": "can open pull requests"}
    if resp.status_code == 403:
        return {"repo": repo, "ok": False,
                "detail": "the token reaches this repo but lacks Pull requests: Read and write"}
    if resp.status_code == 404:
        return {"repo": repo, "ok": False,
                "detail": "the token cannot see this repo — add it to the token's repository "
                          "selection, and check its Resource owner is the repo's org"}
    return {"repo": repo, "ok": False, "detail": f"GitHub answered {resp.status_code}"}


def probe_push(token: str, repo: str) -> dict:
    """May this token push to `repo`? Creates nothing — see `_PROBE_SHA`."""
    try:
        resp = requests.post(
            f"{GITHUB_API}/repos/{repo}/git/refs",
            headers=github_headers(token),
            json={"ref": f"refs/heads/{_PROBE_HEAD}", "sha": _PROBE_SHA},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        return {"repo": repo, "ok": False, "detail": f"could not reach GitHub: {exc}"}
    if resp.status_code == 422:
        return {"repo": repo, "ok": True, "detail": "can push"}
    if resp.status_code in (403, 404):
        return {"repo": repo, "ok": False,
                "detail": "the token cannot push here — it needs Contents: Read and write, "
                          "and this repo in its repository selection"}
    return {"repo": repo, "ok": False, "detail": f"GitHub answered {resp.status_code}"}


def check_repo(token: str, repo: str) -> dict:
    """Can this token SHIP to `repo` — push a branch, then open a pull request?
    One row per repo; the first thing it cannot do is the detail."""
    pr = probe_pull_request(token, repo)
    if not pr["ok"]:
        return pr
    push = probe_push(token, repo)
    if not push["ok"]:
        return push
    return {"repo": repo, "ok": True, "detail": "can push and open pull requests"}


def _meta_for(token: str, agent: Agent) -> dict:
    meta = inspect_github(token)
    repo = agent_repo(agent)
    meta["checks"] = [check_repo(token, repo)] if repo else []
    meta["checked_at"] = timezone.now().isoformat()
    return meta


def _expires(meta: dict) -> dt.datetime | None:
    raw = meta.get("expires_at")
    return dt.datetime.fromisoformat(raw) if raw else None


# ---- the delegation itself ---------------------------------------------------

def github_entry(agent: Agent) -> AgentCredential | None:
    """The GitHub token a person delegated to `agent`, in its vault (`accounts`): sealed
    to the machines that run the agent, readable by no person. None before one is given."""
    return accounts.delegated_entries(agent).filter(name=accounts.GITHUB_ENTRY,
                                                    provider="github").first()


def set_github(agent: Agent, user, token: str):
    """Seal `user`'s GitHub token into `agent`'s vault as a credential delegated by them,
    after proving it works. Replaces the one delegated now, whoever gave it.

    The agent's owner or one of its admins may delegate (`Agent.is_admin`). Refused rather
    than stored when the token cannot push to and open a pull request on the agent's own
    repo — the commonest mistake and the cheapest moment to say so — and (409
    `no_machine_can_open`) when no machine that runs the agent can open it yet.

    canopy-web sees the token for this request only: it checks it, seals it to the
    machines that run the agent (and to the person's own vault key), and keeps no copy it
    can read."""
    token = (token or "").strip()
    if not token:
        raise DelegationError("paste the token")
    if not agent.is_admin(user):
        raise DelegationError(
            f"only {agent.slug}'s owner or an admin can delegate their GitHub identity to it")
    meta = _meta_for(token, agent)
    failed = [c for c in meta["checks"] if not c["ok"]]
    if failed:
        raise DelegationError(f"{failed[0]['repo']}: {failed[0]['detail']}")
    name = accounts.GITHUB_ENTRY
    with transaction.atomic():
        agent = zk._locked(agent)
        own = AgentCredential.objects.filter(agent=agent, name=name, account="agent").first()
        if own is not None:
            raise DelegationError(
                f"{agent.slug} already holds a credential of its own named {name}: rename or remove "
                "it first")
        value_zk, _ = accounts.seal(agent, name, token, user)
        entry, _ = AgentCredential.objects.update_or_create(
            agent=agent, name=name,
            defaults={
                "value_zk": value_zk, "kv": agent.zk_kv, "updated_by": user,
                "account": "delegated", "delegated_by": user, "delegated_by_agent": None,
                "provider": "github",
                "identity": {k: meta.get(k) for k in ("login", "user_id", "name")},
                "purpose": f"{agent.slug} pushes and opens pull requests"
                           + (f" on {agent_repo(agent)}" if agent_repo(agent) else ""),
                "scopes": [c["repo"] for c in meta["checks"]],
                "provider_expires_at": _expires(meta),
                "checks": meta["checks"], "checked_at": timezone.now(), "checked_by": "server",
            })
    return entry


def record_checks(agent: Agent, name: str, *, checks: list, provider_expires_at=None,
                  error: str = "", by: str) -> AgentCredential | None:
    """What a machine that opened a delegated credential found when it probed it. canopy-web
    cannot probe one itself — it cannot open it."""
    entry = accounts.delegated_entries(agent).filter(name=name).first()
    if entry is None:
        return None
    rows = [{"repo": str(c.get("repo") or "")[:200], "ok": bool(c.get("ok")),
             "detail": str(c.get("detail") or "")[:300]} for c in (checks or [])[:20]]
    if error:
        rows = [{"repo": agent_repo(agent), "ok": False, "detail": str(error)[:300]}]
    entry.checks, entry.checked_at, entry.checked_by = rows, timezone.now(), by[:120]
    fields = ["checks", "checked_at", "checked_by", "updated_at"]
    if provider_expires_at is not None:
        entry.provider_expires_at = provider_expires_at
        fields.append("provider_expires_at")
    entry.save(update_fields=fields)
    return entry


def check_github(agent: Agent):
    """The agent's GitHub entry. canopy-web cannot probe one itself — it cannot open it — so
    the machines that do record what they found (`record_checks`)."""
    return github_entry(agent)


def clear_github(agent: Agent, user) -> bool:
    """Withdraw the caller's own GitHub delegation."""
    entry = github_entry(agent)
    if entry is None or entry.delegated_by_id != user.pk:
        return False
    deleted, _ = AgentCredential.objects.filter(pk=entry.pk).delete()
    return bool(deleted)


def status(agent: Agent, *, machines: bool = False, viewer=None) -> dict:
    """Everything the settings screen shows. Never the token. `machines`: also which
    machines can open it — read for the agent's own GitHub panel only, never by the
    health checks that ask this of every agent in a tree."""
    owner = agent.owner
    base = {
        "repo": agent_repo(agent),
        "owner_email": owner.email if owner else "",
        "lent_by_email": "",
        "create_url": create_url(agent),
        "set": False,
        # Repos whose agentless project turns borrow this identity (RepoIdentity).
        "identity_for_repos": list(
            RepoIdentity.objects.filter(agent=agent).values_list("repo_slug", flat=True)),
    }
    if machines:
        # Who could open a token `viewer` gave now: said before they paste one.
        base["eligible_machines"] = sorted({k.runner.name for k in accounts.delegated_devices(agent, viewer)})
    now = timezone.now()
    entry = github_entry(agent)
    if entry is not None:
        about = (accounts.describe(entry, agent) if machines else
                 {"in_force": accounts.serves(entry, agent), "label": accounts.label(entry, agent),
                  "delegated_by_email": entry.delegated_by.email if entry.delegated_by_id else "",
                  "identity": entry.identity or {}, "checks": entry.checks or [],
                  "machines": [], "waiting": []})
        failed = next((c for c in about["checks"] if not c.get("ok", True)), None)
        expires = entry.provider_expires_at
        return {
            **base,
            "set": about["in_force"],
            "lent_by_email": about["delegated_by_email"],
            "login": about["identity"].get("login", ""),
            "name": about["identity"].get("name", ""),
            "expires_at": expires,
            "expired": bool(expires and expires <= now),
            "expiring_soon": bool(expires and now < expires <= now + EXPIRY_WARNING),
            "checks": about["checks"],
            "error": ("" if about["in_force"] else
                      f"{about['delegated_by_email']} is no longer an admin of {agent.slug}, so their "
                      "token is not used — they or another admin delegate one again"),
            "checked_at": entry.checked_at.isoformat() if entry.checked_at else None,
            "checked_by": entry.checked_by,
            "updated_at": entry.updated_at,
            "in_vault": True,
            "account_label": about["label"],
            "machines": about["machines"],
            "waiting_machines": about["waiting"],
            "no_longer_works": bool(failed),
        }
    return base


def readiness(agent: Agent) -> tuple[str, str]:
    """`(status, detail)` for the `github.<slug>` health check of a box that runs
    this agent — the one verdict both the box's boot-time ask and every read of
    the runner's health give. Reads the stored checks; never calls GitHub."""
    st = status(agent)
    if not st["set"]:
        return "fail", st.get("error") or (
            f"neither owner {st['owner_email'] or '(none)'} nor an admin has delegated {agent.slug} "
            f"a GitHub token — /agents/{agent.slug}/settings → External accounts → GitHub")
    if st["expired"]:
        return "fail", f"GitHub says the token expired {st['expires_at']:%Y-%m-%d} — replace it"
    if st["error"]:
        return "fail", st["error"]
    bad = next((c for c in st["checks"] if not c["ok"]), None)
    if bad:
        return "fail", f"{bad['repo']}: {bad['detail']} — this token no longer works; replace it"
    if st["expiring_soon"]:
        return "warn", f"GitHub says the token expires {st['expires_at']:%Y-%m-%d} — replace it soon"
    return "ok", f"acts as @{st['login']}" + (
        f", can push and open pull requests on {st['repo']}" if st["repo"] else "")


# ---- handing it to a turn ----------------------------------------------------

def turn_agent(turn) -> Agent | None:
    """The agent a turn runs AS: its own for an agent turn, the session's for a
    chat with an agent, a project turn's or project chat's configured fallback
    if its repo has one, else none (a project with no default set). The same
    decision the runner makes in `_turn_agent_slug`, made here from the rows —
    except the project fallback, which the runner cannot make: it would need
    to know the repo's configured agent (`RepoIdentity`), and only canopy-web
    does."""
    if turn.agent_id:
        return turn.agent
    cs = getattr(turn, "chat_session", None)
    if cs is not None and cs.agent_id:
        return cs.agent
    # A project chat's repo is on its SESSION: a chat turn's own `project` is
    # always "" (`turn_targets_agent_xor_project_xor_session`), so reading
    # `turn.project` alone refused every project chat a GitHub identity.
    repo = turn.project or (cs.project if cs is not None else "")
    if repo:
        row = RepoIdentity.objects.select_related("agent").filter(repo_slug=repo).first()
        if row is not None:
            return row.agent
    return None


def requested_by(turn) -> str:
    """`Name <email>` of whoever caused this turn, for a `Requested-by:` trailer —
    "" when nobody in particular did (a schedule, a drill)."""
    for person, name_attr in ((turn.initiator_user, None), (turn.initiator_contact, "display_name")):
        if person is None:
            continue
        email = getattr(person, "email", "") or ""
        name = (getattr(person, name_attr, "") if name_attr else person.get_full_name()) or email
        return f"{name} <{email}>" if email and name != email else email or name
    return ""


def github_token_for_turn(turn) -> dict:
    """The git identity to commit with for one turn, and where its token is.

    A token a person delegated to the agent is in its vault, sealed to the machines that
    run it: this hands out no token, only `delegated` (the vault entry's name) and
    `agent` — the box opens the entry from its own resolve. `credentials/resolve` never
    returns it to a person."""
    if turn.capability:
        # A CALLER's turn (confined to a capability of the agent's interface) is
        # never lent the owner's GitHub identity: no published capability needs it,
        # and in a session a stranger steers it is the most valuable thing on the
        # box. The runner does not ask for one either; this is the server's half,
        # so a runner that did would still get nothing.
        raise DelegationError(
            f"this turn is confined to the caller capability '{turn.capability}', and a "
            f"caller's turn is never given its agent owner's GitHub identity")
    agent = turn_agent(turn)
    if agent is None:
        raise DelegationError("this turn runs as no agent, so it has no GitHub identity")
    entry = github_entry(agent)
    if entry is not None:
        if not accounts.serves(entry, agent):
            raise DelegationError(
                f"{entry.delegated_by.email if entry.delegated_by_id else 'its person'} is no longer an "
                f"admin of {agent.slug}, so the GitHub token they delegated is not used — delegate one "
                f"at /agents/{agent.slug}/settings → External accounts → GitHub")
        ident = entry.identity or {}
        return _issued(turn, agent, token="", login=ident.get("login", ""), user_id=ident.get("user_id", 0),
                       name=ident.get("name", ""), expires_at=entry.provider_expires_at,
                       delegated=entry.name)
    who = agent.owner.email if agent.owner else "nobody (the agent has no owner)"
    raise DelegationError(
        f"neither {agent.slug}'s owner {who} nor an admin has delegated it a GitHub identity — "
        f"add a token at /agents/{agent.slug}/settings → External accounts → GitHub")


def _issued(turn, agent: Agent, *, token: str, login: str, user_id, name: str, expires_at,
            delegated: str) -> dict:
    return {
        "token": token,
        "delegated": delegated,
        "agent": agent.slug,
        "expires_at": expires_at,
        "github_login": login,
        "git_name": name or login,
        # GitHub's noreply address links a commit to the account without
        # publishing anyone's email, and the fine-grained token cannot read the
        # real one anyway.
        "git_email": f"{user_id}+{login}@users.noreply.github.com" if login else "",
        "repo": agent_repo(agent),
        "requested_by": requested_by(turn),
    }
