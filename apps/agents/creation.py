"""Scaffold a new agent's repository from the browser — the factory, pushed over the GitHub API.

The repo is created with its credential replaced by the one agents actually use: the owner's fine-grained
token (`apps/agents/credentials/delegations.py`). The person makes an empty repo
and a token scoped to it; this pushes with that token, and the same token is then saved
as the agent's GitHub delegation — so the push and every later turn use one credential,
one repository selection, and no app install an org owner must approve.

Three things it does NOT do, each on purpose:

- **It does not own the scaffold.** Every file comes from `canopy-agent-factory`, the
  package `/canopy:create-agent` runs in a terminal (`tests/test_agent_factory_contract.py`
  pins the surface used here).
- **It does not create the repository.** No token canopy is handed should be able to;
  the person creates a blank repo and this only pushes into it.
- **It does not register the agent or store the token.** Registration is
  `POST /api/agents/` and the delegation is `PUT /api/agents/{slug}/github` — the same
  audited, gated routes everything else uses — so this module holds no second copy of
  who may create an agent or lend it an identity.

Retry-safe, and safe over earlier work: a repo that already holds this agent's scaffold
(an earlier run here, or `/canopy:create-agent` in a terminal) — or only GitHub's own
starter files — gets just the template files it is MISSING, in one commit on top, and
nothing in it is ever overwritten. A repo that is some other agent, or not an agent at
all, is refused rather than written into.
"""
from __future__ import annotations

import base64
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests

from apps.contacts.hcp.sensitive import _context_turn
from apps.harness import initiator as who
from apps.harness import turn_asker
from apps.harness.models import Turn
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from .credentials import delegations
from .models import Agent

GITHUB_API = delegations.GITHUB_API
HTTP_TIMEOUT = delegations.HTTP_TIMEOUT
#: The factory writes this; its `name` is the agent slug. How a retry recognises its own work.
PLUGIN_MANIFEST = ".claude-plugin/plugin.json"
#: Written first, through the Contents API — the one GitHub call that can make the first
#: commit in an EMPTY repository (the Git Data API answers 409 until a commit exists).
FIRST_FILE = "README.md"
_REPO_RE = re.compile(r"^([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})$")


class ScaffoldError(Exception):
    """A person must fix something. The message is shown as-is."""

    def __init__(self, message: str, *, status: int = 409):
        super().__init__(message)
        self.status = status


#: What GitHub's "Initialize this repository with…" boxes add. A repo holding only
#: these was made for the agent by someone who ticked one, so it is filled in, not refused.
GITHUB_STARTER_FILES = {"README.md", "LICENSE", "LICENSE.md", ".gitignore"}


@dataclass
class RepoState:
    """What `repo` looks like through the person's token."""

    repo: str
    #: ready — empty; update — this agent's scaffold (or only GitHub starter files)
    #: with template files missing; adopt — nothing missing; name_mismatch — the
    #: scaffold of a DIFFERENT agent slug; not_empty — not an agent repo;
    #: missing — the token cannot see it.
    state: str
    default_branch: str = "main"
    detail: str = ""
    #: Template paths the repo lacks — what an `update` will add. Nothing else is written.
    missing: tuple[str, ...] = ()
    #: For name_mismatch: the slug this repo already names its agent.
    suggested_slug: str = ""
    head_sha: str = ""
    base_tree: str = ""


#: Slugs the SPA needs for itself: `/w/:ws/agents/new` is the create page, and
#: `scaffold` / `stored` are create routes under `/api/agents/`.
RESERVED_SLUGS = frozenset({"new", "scaffold", "stored"})


#: How sure canopy must be of a person before an agent's register makes THEM the owner:
#: signed in, or on their own token / private runner — never a weaker claim.
OWNER_ASSURANCES = ("session", "pat", "private-runner-key")


class NoOwnerError(ScaffoldError):
    """An agent's login creates an agent no person can be made the owner of."""


def creating_owner(request, home):
    """Who owns an agent this request creates. A person creates for themselves. An AGENT's
    own login creates for a person, never for itself (an agent owning an agent leaves no
    one accountable for it):

    1. the person who asked for the turn it works in, when canopy-web can tell which turn
       that is and they may create agents in `home` (`_turn_asker`);
    2. else the agent's own owner, when they may create agents in `home`;
    3. else NoOwnerError (403): no one to own it.

    The turn comes from the login itself where it can — the login canopy-web issued for a
    turn, or for a laptop session (`turn_asker.check`) — and else from the parent turn the
    request names (`X-Canopy-Parent-Turn`, which the canopy CLI and the canopy MCP server's
    headers helper send), checked, not taken on trust: live, this agent's own, and asked by a
    person canopy verified. A stored token alone (an agent's PAT) carries no turn."""
    caller = request.user
    agent = _agent_of_login(caller)
    if agent is None:
        return caller
    person = _turn_asker(request, caller, agent)
    if person is not None and _may_create(person, home):
        return person
    owner = agent.owner
    if owner is not None and _agent_of_login(owner) is None and _may_create(owner, home):
        return owner
    raise NoOwnerError(
        f"{agent.slug} is an agent: what it creates is owned by a person — the one who asked for "
        f"its turn, or else its owner — and neither may create agents in '{home.slug}'. Create it "
        f"from a turn a member who may create agents there asked for, or have a person create it.",
        status=403)


def _agent_of_login(user):
    """The agent `user` is the own login of (`initiator._agent_login_of`), or None."""
    slug = who._agent_login_of(user) if user is not None else None
    return Agent.objects.filter(slug=slug).select_related("owner").first() if slug else None


def _may_create(person, home) -> bool:
    return bool(getattr(person, "is_active", False)) and perms.role_allows(
        wsvc.member_role(person, home), perms.AGENT_WORK)


def _turn_asker(request, caller, agent):
    """The person who asked for the live turn `caller` (`agent`'s login) works in, or None."""
    try:
        ob = turn_asker.check(caller, getattr(request, "auth_credential", None), session_login_live=True)
    except turn_asker.Refused:
        ob = None
    if ob is not None and ob.agent.pk == agent.pk:
        return ob.user
    turn = _context_turn()
    if (turn is None or turn.agent_id != agent.pk
            or turn.status not in (Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN)
            or turn.initiator_kind != who.USER or turn.initiator_user_id is None
            or turn.initiator_assurance not in OWNER_ASSURANCES):
        return None
    return turn.initiator_user


def expected_paths(slug: str) -> set[str]:
    """Every path the factory writes for `slug` — from its own stamp table."""
    from canopy_agent_factory import templates

    return {p.replace("{{AGENT_SLUG}}", slug) for p in templates()}


def parse_repo(raw: str) -> str:
    """`owner/name` from `owner/name` or any github.com URL, else ScaffoldError(422)."""
    text = (raw or "").strip().removesuffix(".git").rstrip("/")
    text = re.sub(r"^(https?://)?(www\.)?github\.com/", "", text)
    if not _REPO_RE.match(text):
        raise ScaffoldError(f"{raw!r} is not a GitHub repository — use owner/name", status=422)
    return text


def repo_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def new_repo_url(owner: str, name: str) -> str:
    """GitHub's new-repository form, filled in. Blank, private, no README — it must be empty."""
    from urllib.parse import urlencode

    return f"https://github.com/new?{urlencode({'owner': owner, 'name': name, 'visibility': 'private'})}"


def _get(token: str, path: str) -> requests.Response:
    return requests.get(f"{GITHUB_API}{path}", headers=delegations.github_headers(token), timeout=HTTP_TIMEOUT)


def _send(token: str, method: str, path: str, body: dict) -> dict:
    resp = requests.request(method, f"{GITHUB_API}{path}", headers=delegations.github_headers(token),
                            json=body, timeout=HTTP_TIMEOUT)
    if resp.status_code in (403, 404):
        raise ScaffoldError(
            f"GitHub refused the write ({resp.status_code}). The token needs Contents and "
            "Workflows: Read and write on this repository.")
    if resp.status_code == 422 and "/git/refs/heads/" in path:
        raise ScaffoldError(
            "GitHub would not move the default branch — it is probably protected. Allow "
            "the token to push to it, or push the scaffold on a branch yourself.")
    if not resp.ok:
        raise ScaffoldError(f"GitHub answered {resp.status_code}: {resp.text[:200]}", status=502)
    return resp.json()


def inspect_repo(token: str, repo: str, slug: str) -> RepoState:
    """Can canopy push this agent's scaffold into `repo`? Reads only."""
    resp = _get(token, f"/repos/{repo}")
    if resp.status_code == 401:
        raise ScaffoldError("GitHub rejected the token (401): it is wrong, expired or revoked.",
                            status=422)
    if resp.status_code in (403, 404):
        # 404 both for a repo that does not exist and for one outside the token's
        # repository selection — GitHub will not say which, so the message names both.
        return RepoState(repo, "missing", detail=(
            f"The token cannot see {repo}. Create the repo if you have not, and make sure "
            "the token's Resource owner is its owner and its repositories include it."))
    resp.raise_for_status()
    branch = resp.json().get("default_branch") or "main"
    commits = _get(token, f"/repos/{repo}/commits?per_page=1")
    if commits.status_code == 409:  # "Git Repository is empty."
        return RepoState(repo, "ready", branch)
    commits.raise_for_status()
    head = commits.json()[0]
    head_sha, base_tree = head["sha"], head["commit"]["tree"]["sha"]
    tree = _get(token, f"/repos/{repo}/git/trees/{base_tree}?recursive=1")
    tree.raise_for_status()
    existing = {e["path"] for e in tree.json().get("tree", []) if e.get("type") == "blob"}
    name = _manifest_name(token, repo) if PLUGIN_MANIFEST in existing else None
    if name and name != slug:
        return RepoState(repo, "name_mismatch", branch, suggested_slug=name, detail=(
            f"{repo} already holds the agent '{name}'. Use '{name}' as the slug to finish "
            f"setting it up, or pick another repository."))
    if not (name == slug or existing <= GITHUB_STARTER_FILES):
        return RepoState(repo, "not_empty", branch, detail=(
            f"{repo} already has other content and is not an agent repository. Use a new, "
            "empty repository (or one that only has a README, licence or .gitignore)."))
    missing = tuple(sorted(expected_paths(slug) - existing))
    kept = len(expected_paths(slug) & existing)
    if not missing:
        return RepoState(repo, "adopt", branch, detail=f"{repo} already holds {slug}'s scaffold.")
    return RepoState(repo, "update", branch, missing=missing, head_sha=head_sha, base_tree=base_tree,
                     detail=(f"{repo} already has {len(existing)} file(s). Canopy will add the "
                             f"{len(missing)} template file(s) it lacks"
                             + (f" and keep its {kept} existing one(s) as they are." if kept else
                                " and leave everything already there alone.")))


def _manifest_name(token: str, repo: str) -> str | None:
    """The `name` in the repo's `.claude-plugin/plugin.json` — the agent it already is."""
    resp = _get(token, f"/repos/{repo}/contents/{PLUGIN_MANIFEST}")
    if not resp.ok:
        return None
    try:
        return json.loads(base64.b64decode(resp.json().get("content", ""))).get("name") or None
    except (ValueError, TypeError, AttributeError):
        return None


def render(spec) -> dict[str, tuple[str, bool]]:
    """{repo-relative path: (content, executable)} — exactly what the factory writes."""
    from canopy_agent_factory import create_agent

    with tempfile.TemporaryDirectory() as tmp:
        root = (Path(tmp) / spec.slug).resolve()  # the factory resolves its target
        written = create_agent(spec, root)
        return {
            p.relative_to(root).as_posix(): (p.read_text(encoding="utf-8"), bool(p.stat().st_mode & 0o111))
            for p in written
        }


def push(token: str, state: RepoState, files: dict[str, tuple[str, bool]], *, slug: str) -> int:
    """Write the scaffold; returns how many files were written.

    `ready` (empty): two commits — the Git Data API (the only way to write many files
    with their modes at once) refuses an empty repository, so the first commit comes
    from the Contents API, which writes one file and no mode.
    `update`: one commit on top of the branch with only `state.missing`; every file
    already in the repo is left exactly as it is."""
    if state.state not in ("ready", "update"):
        raise ScaffoldError(f"nothing to push into a repository in state {state.state!r}")
    repo, branch = state.repo, state.default_branch
    if state.state == "update":
        todo = {p: files[p] for p in state.missing if p in files}
        _commit(token, repo, branch, todo, parent=state.head_sha, base_tree=state.base_tree,
                message=f"Add the canopy agent scaffold's missing files for {slug} ({len(todo)})")
        return len(todo)
    readme, _ = files[FIRST_FILE]
    first = _send(token, "PUT", f"/repos/{repo}/contents/{FIRST_FILE}", {
        "message": f"Scaffold {slug}: README",
        "content": base64.b64encode(readme.encode("utf-8")).decode("ascii"),
    })["commit"]
    rest = {p: f for p, f in files.items() if p != FIRST_FILE}
    _commit(token, repo, branch, rest, parent=first["sha"], base_tree=first["tree"]["sha"],
            message=f"Scaffold {slug} from the canopy agent factory")
    return len(files)


def _commit(token: str, repo: str, branch: str, files: dict[str, tuple[str, bool]], *,
            parent: str, base_tree: str, message: str) -> str:
    """One commit adding `files` (with their modes) on top of `parent`; moves `branch`."""
    tree = _send(token, "POST", f"/repos/{repo}/git/trees", {
        "base_tree": base_tree,
        "tree": [
            {"path": path, "mode": "100755" if exe else "100644", "type": "blob", "content": text}
            for path, (text, exe) in sorted(files.items())
        ],
    })
    commit = _send(token, "POST", f"/repos/{repo}/git/commits",
                   {"message": message, "tree": tree["sha"], "parents": [parent]})
    _send(token, "PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": commit["sha"]})
    return commit["sha"]
