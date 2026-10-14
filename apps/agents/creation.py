"""Scaffold a new agent's repository from the browser — the factory, pushed over the GitHub API.

The design is `docs/superpowers/specs/2026-09-12-github-backed-agent-creation-design.md`;
this is its last missing piece. Three things it does NOT do, each on purpose:

- **It does not own the scaffold.** Every file comes from `canopy-agent-factory`, the
  package `/canopy:create-agent` runs in a terminal. The browser and Claude Code stamp
  the same agent because there is one stamp table (`tests/test_agent_factory_contract.py`
  pins the surface used here).
- **It does not create the repository.** The GitHub App holds no `Administration`
  permission (see `apps/tokens/github_app.py`), so the person makes a blank repo and adds
  it to the installation; this only pushes into it.
- **It does not register the agent.** Registration is `POST /api/agents/` — the same
  audited, role-gated upsert the plugin uses — so a web-created agent and a terminal-created
  one arrive through one door. Keeping the push out of that route also keeps it out of the
  settings-audit transaction: a refresh rotates the person's GitHub refresh token, and a
  rollback after a failed push would throw the rotated token away and kill their grant.

Retry-safe: a repo that already holds THIS agent's scaffold is adopted, not refused.
"""
from __future__ import annotations

import base64
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests

from apps.tokens import github_app

GITHUB_API = github_app.GITHUB_API
HTTP_TIMEOUT = github_app.HTTP_TIMEOUT
#: The factory writes this; its `name` is the agent slug. How a retry recognises its own work.
PLUGIN_MANIFEST = ".claude-plugin/plugin.json"
#: Written first, through the Contents API — the one GitHub call that can make the first
#: commit in an EMPTY repository (the Git Data API answers 409 until a commit exists).
FIRST_FILE = "README.md"
_REPO_RE = re.compile(r"^([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})$")


class ScaffoldError(Exception):
    """A person must fix something. The message is shown as-is."""

    def __init__(self, message: str, *, status: int = 409, fix_url: str = ""):
        super().__init__(message)
        self.status = status
        self.fix_url = fix_url


@dataclass
class RepoState:
    """What canopy can see of `repo` with the person's own GitHub grant."""

    repo: str
    #: ready — empty and writable; adopt — already this agent's scaffold;
    #: not_empty — has someone else's commits; missing — canopy cannot see it.
    state: str
    default_branch: str = "main"
    detail: str = ""
    fix_url: str = ""


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


def install_settings_url() -> str:
    """Where the person adds a repo to canopy's installation. GitHub's install page for the
    app shows a configure screen for an account it is already installed on."""
    slug = github_app.app_slug()
    return f"https://github.com/apps/{slug}/installations/new" if slug else "https://github.com/settings/installations"


def _get(token: str, path: str) -> requests.Response:
    return requests.get(f"{GITHUB_API}{path}", headers=github_app.auth_headers(token), timeout=HTTP_TIMEOUT)


def _send(token: str, method: str, path: str, body: dict) -> dict:
    resp = requests.request(method, f"{GITHUB_API}{path}", headers=github_app.auth_headers(token),
                            json=body, timeout=HTTP_TIMEOUT)
    if resp.status_code in (403, 404):
        raise ScaffoldError(
            f"GitHub refused to write to the repository ({resp.status_code}). Add it to "
            "canopy's GitHub access and try again.", fix_url=install_settings_url())
    if not resp.ok:
        raise ScaffoldError(f"GitHub answered {resp.status_code}: {resp.text[:200]}", status=502)
    return resp.json()


def inspect_repo(token: str, repo: str, slug: str) -> RepoState:
    """Can canopy push this agent's scaffold into `repo`? Reads only."""
    resp = _get(token, f"/repos/{repo}")
    if resp.status_code in (403, 404):
        # 404 for a repo that does not exist AND for one that exists but was not added
        # to the installation — GitHub will not say which. The second is the step people
        # forget (the spec's "middle step"), so the message names it.
        return RepoState(repo, "missing", detail=(
            f"canopy cannot see {repo}. If you just made it, add it to canopy's GitHub "
            "access (a repo you create is not added automatically)."),
            fix_url=install_settings_url())
    resp.raise_for_status()
    branch = resp.json().get("default_branch") or "main"
    commits = _get(token, f"/repos/{repo}/commits?per_page=1")
    if commits.status_code == 409:  # "Git Repository is empty."
        return RepoState(repo, "ready", branch)
    manifest = _get(token, f"/repos/{repo}/contents/{PLUGIN_MANIFEST}")
    if manifest.ok:
        try:
            name = json.loads(base64.b64decode(manifest.json().get("content", ""))).get("name")
        except (ValueError, TypeError):
            name = None
        if name == slug:
            return RepoState(repo, "adopt", branch, detail=f"{repo} already holds {slug}'s scaffold.")
    return RepoState(repo, "not_empty", branch, detail=(
        f"{repo} already has commits. Use a new, empty repository — no README, licence or "
        ".gitignore — so the scaffold is its first commit."))


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


def push(token: str, state: RepoState, files: dict[str, tuple[str, bool]], *, slug: str) -> str:
    """Make the scaffold the repo's history: two commits, returns the head sha.

    One commit is not possible on an empty repository — the Git Data API (the only way to
    write many files and their modes at once) refuses until a commit exists, and the
    Contents API that can make that first commit writes one file and no mode."""
    repo, branch = state.repo, state.default_branch
    readme, _ = files[FIRST_FILE]
    first = _send(token, "PUT", f"/repos/{repo}/contents/{FIRST_FILE}", {
        "message": f"Scaffold {slug}: README",
        "content": base64.b64encode(readme.encode("utf-8")).decode("ascii"),
    })["commit"]
    tree = _send(token, "POST", f"/repos/{repo}/git/trees", {
        "base_tree": first["tree"]["sha"],
        "tree": [
            {"path": path, "mode": "100755" if exe else "100644", "type": "blob", "content": text}
            for path, (text, exe) in sorted(files.items()) if path != FIRST_FILE
        ],
    })
    commit = _send(token, "POST", f"/repos/{repo}/git/commits", {
        "message": f"Scaffold {slug} from the canopy agent factory",
        "tree": tree["sha"],
        "parents": [first["sha"]],
    })
    _send(token, "PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": commit["sha"]})
    return commit["sha"]
