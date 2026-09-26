"""A person lending their GitHub identity to one agent — see `AgentDelegation`.

Request-free service layer: the agent routes (set / check / clear), the harness
runner route (issue one token to one turn) and the runner-readiness route all
call these, so they cannot drift about which delegation is in force.

THE ONE RULE: a turn's GitHub credential is its agent owner's delegation to that
agent, resolved here, server-side. The runner names a turn it has claimed and
nothing else — it never chooses whose identity to use, and there is no fallback
to any shared token when the delegation is missing. A turn with no agent (a repo
chat, a project turn) has no GitHub credential at all.

The caller of a turn (the person who emailed the agent, say) is deliberately NOT
the principal: an agent's GitHub writes use its owner's identity, whoever caused
them (Jonathan, 2026-09-24). Stopping an agent from doing something bad is the
agent's own design responsibility — its gating hooks and sender triage — not the
token layer's. Who asked is RECORDED instead (`requested_by`), so attribution
shows both the credential and the person who caused the change.
"""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlencode

import requests
from django.utils import timezone

from apps.common.encryption import decrypt_secret, encrypt_secret

from .definition import definition_key
from .models import Agent, AgentDelegation

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
    repo = agent_repo(agent)
    params = {
        "name": f"canopy {agent.slug}"[:40],
        "description": f"canopy-web agent '{agent.slug}' acting as you on GitHub",
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

def _headers(token: str) -> dict:
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
    GitHub rejects it outright."""
    try:
        resp = requests.get(f"{GITHUB_API}/user", headers=_headers(token), timeout=HTTP_TIMEOUT)
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
            headers=_headers(token),
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
            headers=_headers(token),
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

def delegation_for(agent: Agent, service: str = AgentDelegation.GITHUB) -> AgentDelegation | None:
    """The delegation IN FORCE for this agent: its current owner's. A row left
    behind by a previous owner is not it."""
    if agent.owner_id is None:
        return None
    return AgentDelegation.objects.filter(
        agent=agent, user_id=agent.owner_id, service=service).first()


def set_github(agent: Agent, user, token: str) -> AgentDelegation:
    """Store `user`'s GitHub token for `agent`, after proving it works.

    Only the agent's OWNER may delegate: a delegation from anyone else would
    never be used (see `delegation_for`), and accepting one would read as if it
    were. Refused rather than stored when the token cannot open a pull request
    on the agent's own repo — a token that fails its first check is the most
    common mistake (wrong resource owner, repo not selected) and the cheapest
    moment to say so.
    """
    token = (token or "").strip()
    if not token:
        raise DelegationError("paste the token")
    if agent.owner_id != getattr(user, "pk", None):
        raise DelegationError(
            f"only {agent.slug}'s owner can lend it their GitHub identity")
    meta = _meta_for(token, agent)
    failed = [c for c in meta["checks"] if not c["ok"]]
    if failed:
        raise DelegationError(f"{failed[0]['repo']}: {failed[0]['detail']}")
    row, _ = AgentDelegation.objects.update_or_create(
        user=user, agent=agent, service=AgentDelegation.GITHUB,
        defaults={"secret_enc": encrypt_secret(token), "meta": meta, "expires_at": _expires(meta)},
    )
    return row


def check_github(agent: Agent) -> AgentDelegation | None:
    """Re-run the checks on the delegation in force, and record what they found."""
    row = delegation_for(agent)
    if row is None:
        return None
    try:
        meta = _meta_for(decrypt_secret(row.secret_enc), agent)
    except DelegationError as exc:
        meta = {**row.meta, "checks": [], "checked_at": timezone.now().isoformat(),
                "error": str(exc)}
    row.meta = meta
    row.expires_at = _expires(meta) if "error" not in meta else row.expires_at
    row.save(update_fields=["meta", "expires_at", "updated_at"])
    return row


def clear_github(agent: Agent, user) -> bool:
    deleted, _ = AgentDelegation.objects.filter(
        agent=agent, user=user, service=AgentDelegation.GITHUB).delete()
    return bool(deleted)


def status(agent: Agent) -> dict:
    """Everything the settings screen shows. Never the token."""
    row = delegation_for(agent)
    owner = agent.owner
    base = {
        "repo": agent_repo(agent),
        "owner_email": owner.email if owner else "",
        "create_url": create_url(agent),
        "set": row is not None,
    }
    if row is None:
        return base
    meta = row.meta or {}
    now = timezone.now()
    return {
        **base,
        "login": meta.get("login", ""),
        "name": meta.get("name", ""),
        "expires_at": row.expires_at,
        "expired": bool(row.expires_at and row.expires_at <= now),
        "expiring_soon": bool(row.expires_at and now < row.expires_at <= now + EXPIRY_WARNING),
        "checks": meta.get("checks", []),
        "error": meta.get("error", ""),
        "checked_at": meta.get("checked_at"),
        "updated_at": row.updated_at,
    }


# ---- handing it to a turn ----------------------------------------------------

def turn_agent(turn) -> Agent | None:
    """The agent a turn runs AS: its own for an agent turn, the session's for a
    chat with an agent, none for a repo chat or a project turn. The same
    decision the runner makes in `_turn_agent_slug`, made here from the rows."""
    if turn.agent_id:
        return turn.agent
    cs = getattr(turn, "chat_session", None)
    return cs.agent if cs is not None and cs.agent_id else None


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
    """The token, and the git identity to commit with, for one turn."""
    agent = turn_agent(turn)
    if agent is None:
        raise DelegationError("this turn runs as no agent, so it has no GitHub identity")
    row = delegation_for(agent)
    if row is None:
        who = agent.owner.email if agent.owner else "nobody (the agent has no owner)"
        raise DelegationError(
            f"{agent.slug}'s owner {who} has not lent it their GitHub identity — "
            f"add a token at /agents/{agent.slug}/settings → Credentials → GitHub")
    if row.expires_at and row.expires_at <= timezone.now():
        raise DelegationError(
            f"{agent.slug}'s GitHub token expired {row.expires_at:%Y-%m-%d} — its owner "
            f"replaces it at /agents/{agent.slug}/settings → Credentials → GitHub")
    meta = row.meta or {}
    login, user_id = meta.get("login", ""), meta.get("user_id", 0)
    return {
        "token": decrypt_secret(row.secret_enc),
        "expires_at": row.expires_at,
        "github_login": login,
        "git_name": meta.get("name") or login,
        # GitHub's noreply address links a commit to the account without
        # publishing anyone's email, and the fine-grained token cannot read the
        # real one anyway.
        "git_email": f"{user_id}+{login}@users.noreply.github.com" if login else "",
        "repo": agent_repo(agent),
        "requested_by": requested_by(turn),
    }
