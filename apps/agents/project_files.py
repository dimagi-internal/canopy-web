"""A project's files: draft, publish, discard, roll back, read.

A project shares tooling with whoever works on it. Its skills' instructions can live on
`ProjectService` rows; the files those skills run (a script that signs in somewhere, a
query, a template) live here, in a git repository canopy-web hosts for the project
(`models.project_files`, written by `definition_git` with `owner="project_id"`). The
same layout an agent's plugin uses: `skills/<name>/SKILL.md` and the files beside it,
plus `README.md` and `docs/`.

What runs from here runs in the turns of the project's member agents, with the
secrets of every vault attached to the project. So:

* every tree canopy writes is held to the file policy (`check_tree`): text files on the
  editable paths only, no executable bit, no symlink, and nothing shaped like a secret
  (secrets go in the project's vault);
* anyone who may change the project drafts (`gates.may_edit_project`: an editor, a person
  on it, its agents' logins), and a draft replaces the one before it;
* only a PERSON who may change who is on the project — its lead, an admin on it, or an
  owner of its workspace (`project_members.may_manage`), acting as themselves — publishes,
  discards someone else's draft or rolls back. Those are the people the project's vaults
  already open to, so publishing gives nobody a secret they could not read;
* revisions are append-only: nothing here deletes or edits a published tree.

The repository is read through the projects connection (`apps/mcp/project_gateway.py`:
the card lists the files, `project_skill` reads a skill's SKILL.md and names its files,
`project_file` reads one) and the REST routes in `api_project_files.py`.
Doc: `docs/architecture/project-files.md`.
"""
from __future__ import annotations

import re

from canopy_sdk import agent_definition as sdk
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from . import definition_git as git
from .models import Agent, AgentProject, ProjectFilesRevision, ProjectGitObject, ProjectGitRef

DRAFT = ProjectFilesRevision.DRAFT
PUBLISHED = ProjectFilesRevision.PUBLISHED
SUPERSEDED = ProjectFilesRevision.SUPERSEDED
DISCARDED = ProjectFilesRevision.DISCARDED

OWNER = "project_id"

#: The paths a project's repository may hold, as globs, for people (enforced by `editable`).
EDITABLE_GLOBS: tuple[str, ...] = (
    "README.md",
    "docs/**/*.md",
    "skills/<name>/SKILL.md",
    "skills/<name>/**/*.{md,json,yaml,yml,txt,csv,toml,py,sh,js,mjs,ts,sql}",
)
#: What a skill's own files may be: its docs and data, and the scripts it runs. A script
#: is run through its interpreter (`python3 x.py`), never as an executable.
SKILL_EXTS = frozenset({"md", "json", "yaml", "yml", "txt", "csv", "toml", "py", "sh", "js", "mjs", "ts", "sql"})
_SEGMENT = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


class Refused(Exception):
    """A change canopy will not make. `code` is stable for a client; `problems` lists the
    policy's reasons, when the policy refused."""

    def __init__(self, message: str, status: int = 409, *, code: str = "", problems: list | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.problems = problems or []


# --- the file policy ------------------------------------------------------------


def _ext(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def editable(path: str) -> bool:
    """May a project's repository hold `path`?"""
    if sdk.path_problem(path) is not None:
        return False
    parts = path.split("/")
    if not all(_SEGMENT.match(p) for p in parts):
        return False
    if path == "README.md":
        return True
    if parts[0] == "docs":
        return len(parts) >= 2 and _ext(path) == "md"
    if parts[0] == "skills":
        if len(parts) < 3 or not _SKILL_NAME.match(parts[1]):
            return False
        return (len(parts) == 3 and parts[2] == "SKILL.md") or _ext(path) in SKILL_EXTS
    return False


def check_tree(files: dict[str, tuple[bytes, bool]]) -> list[sdk.Problem]:
    """Every reason `files` (`{path: (content, executable)}`) may not be a project's tree."""
    problems: list[sdk.Problem] = []
    total = 0
    for path, (data, executable) in files.items():
        why = sdk.path_problem(path)
        if why is not None:
            problems.append(sdk.Problem(str(path or ""), "bad_path", f"not a valid path: {why}"))
            continue
        if not editable(path):
            problems.append(sdk.Problem(path, "not_editable", "not a path a project's files may hold "
                                        "(README.md, docs/**.md, skills/<name>/SKILL.md and the "
                                        "skill's own text files)"))
            continue
        if executable:
            problems.append(sdk.Problem(path, "executable", "a project's files are never executable; "
                                        "run a script through its interpreter"))
        total += len(data)
        if len(data) > sdk.MAX_FILE_BYTES:
            problems.append(sdk.Problem(path, "too_large", f"larger than {sdk.MAX_FILE_BYTES // 1024} KB"))
            continue
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            problems.append(sdk.Problem(path, "not_utf8", "not UTF-8 text"))
            continue
        if "\x00" in text:
            problems.append(sdk.Problem(path, "not_utf8", "contains NUL"))
            continue
        kinds = sdk.secret_kinds(text)
        if kinds:
            problems.append(sdk.Problem(path, "secret", f"looks like it holds a secret ({', '.join(kinds)}); "
                                        "secrets go in the project's vault, never its files"))
    if len(files) > sdk.MAX_FILES:
        problems.append(sdk.Problem("", "too_many_files", f"more than {sdk.MAX_FILES} files"))
    if total > sdk.MAX_TREE_BYTES:
        problems.append(sdk.Problem("", "tree_too_large",
                                    f"larger than {sdk.MAX_TREE_BYTES // (1024 * 1024)} MB in all"))
    return problems


def _refuse_policy(problems: list[sdk.Problem]) -> Refused:
    first = problems[0]
    more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
    where = f"{first.path}: " if first.path else ""
    return Refused(f"the files break the project file policy — {where}{first.message}{more}", 422,
                   code="policy", problems=[p.as_dict() for p in problems])


# --- reading ---------------------------------------------------------------------


def object_store(project: AgentProject) -> git.AgentObjectStore:
    return git.AgentObjectStore(project.pk, model=ProjectGitObject, owner=OWNER)


def live(project: AgentProject) -> ProjectFilesRevision | None:
    return ProjectFilesRevision.objects.filter(project=project, status=PUBLISHED).first()


def draft(project: AgentProject) -> ProjectFilesRevision | None:
    return ProjectFilesRevision.objects.filter(project=project, status=DRAFT).first()


def revision(project: AgentProject, number: int) -> ProjectFilesRevision | None:
    return ProjectFilesRevision.objects.filter(project=project, number=number).first()


def files_of(rev: ProjectFilesRevision, objects: git.AgentObjectStore | None = None
             ) -> dict[str, tuple[bytes, bool]]:
    return git.files_at(objects or object_store(rev.project), rev.commit)


def read(rev: ProjectFilesRevision, path: str) -> bytes | None:
    """One file of a revision, or None when it holds no such file."""
    if path not in (rev.tree or {}):
        return None
    return git.read_path(object_store(rev.project), rev.commit, path)


def skills(rev: ProjectFilesRevision | None) -> dict[str, dict]:
    """`{name: {"description", "files"}}` of the skills in a revision: every
    `skills/<name>/SKILL.md`, its frontmatter `description:`, and the other files in its
    folder."""
    if rev is None:
        return {}
    paths = sorted(rev.tree or {})
    out: dict[str, dict] = {}
    for path in paths:
        parts = path.split("/")
        if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
            out[parts[1]] = {"description": "", "files": []}
    for path in paths:
        parts = path.split("/")
        if parts[0] == "skills" and parts[1] in out and path != f"skills/{parts[1]}/SKILL.md":
            out[parts[1]]["files"].append(path)
    if out:
        for name, entry in out.items():
            entry["description"] = _description(read(rev, f"skills/{name}/SKILL.md") or b"")
    return out


def _description(skill_md: bytes) -> str:
    m = _FRONTMATTER.match(skill_md.decode("utf-8", errors="replace"))
    if not m:
        return ""
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "description":
            return value.strip().strip("'\"")[:300]
    return ""


def changes_between(old: ProjectFilesRevision | None, new: ProjectFilesRevision) -> list[dict]:
    """`[{path, change}]` (`added`, `modified`, `removed`) from `old`'s tree to `new`'s."""
    before = (old.tree or {}) if old is not None else {}
    after = new.tree or {}
    out = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            out.append({"path": path, "change": "added"})
        elif path not in after:
            out.append({"path": path, "change": "removed"})
        elif before[path].get("sha256") != after[path].get("sha256"):
            out.append({"path": path, "change": "modified"})
    return out


# --- writing ---------------------------------------------------------------------


def _locked(project: AgentProject) -> AgentProject:
    """The project row, locked: numbering and the one-draft rule are decided under it."""
    return AgentProject.objects.select_for_update().get(pk=project.pk)


def _next_number(project: AgentProject) -> int:
    top = ProjectFilesRevision.objects.filter(project=project).aggregate(n=Max("number"))["n"]
    return (top or 0) + 1


def _author(by) -> tuple[bytes, Agent | None]:
    """The commit's author, and the agent whose login `by` is (None for a person)."""
    agent = Agent.objects.filter(user_id=getattr(by, "pk", None)).only("slug").first() \
        if getattr(by, "pk", None) else None
    if agent is not None:
        return git.agent_author(agent.slug), agent
    return git.person_author(by), None


def _new_revision(project: AgentProject, files: dict[str, tuple[bytes, bool]], *, message: str,
                  parent: ProjectFilesRevision | None, by, author: bytes | None = None) -> ProjectFilesRevision:
    """A new revision of `files`, committed on `parent`, as the project's draft."""
    problems = check_tree(files)
    if problems:
        raise _refuse_policy(problems)
    number = _next_number(project)
    message = (message or "").strip()[:4000]
    who, agent = _author(by)
    objects = object_store(project)
    commit = git.make_commit(objects, tree=git.write_tree(objects, files),
                             parents=[parent.commit] if parent is not None else [],
                             author=author or who, message=message or f"Revision {number}")
    objects.flush()
    tree = git.manifest(files)
    rev = ProjectFilesRevision.objects.create(
        project=project, number=number, parent=parent, tree=tree,
        root_sha256=sdk.manifest_root(tree),
        commit=commit, message=message, author_user=by if getattr(by, "pk", None) else None,
        author_agent=agent, status=DRAFT)
    git.set_ref(project.pk, git.DRAFT, commit, model=ProjectGitRef, owner=OWNER)
    return rev


def _apply(base: ProjectFilesRevision | None, changes: list[dict]) -> dict[str, tuple[bytes, bool]]:
    files = dict(files_of(base)) if base is not None else {}
    seen = set()
    for change in changes:
        path = change["path"]
        if path in seen:
            raise Refused(f"{path} is changed twice in one draft", 422, code="duplicate")
        seen.add(path)
        if change.get("content") is None:
            if path not in files:
                raise Refused(f"{path} is not in the live files, so it cannot be deleted", 422, code="not_found")
            files.pop(path)
            continue
        content = change["content"]
        files[path] = (content.encode("utf-8") if isinstance(content, str) else bytes(content), False)
    return dict(sorted(files.items()))


def create_draft(project: AgentProject, *, base: int, changes: list[dict], message: str, by) -> ProjectFilesRevision:
    """The project's one draft, made or replaced: `changes` (`[{path, content | None}]`,
    None deletes) applied to the LIVE revision `base` (0 when nothing is live yet)."""
    if not changes:
        raise Refused("a draft needs at least one change", 422, code="empty")
    with transaction.atomic():
        project = _locked(project)
        live_rev = live(project)
        if base != (live_rev.number if live_rev is not None else 0):
            raise Refused(f"revision {base} is not live; the live revision is "
                          f"{live_rev.number if live_rev is not None else 'none (use 0)'}", 409, code="stale_base")
        old = draft(project)
        if old is not None:
            _discard(old)
        rev = _new_revision(project, _apply(live_rev, changes), message=message, parent=live_rev, by=by)
        if live_rev is not None and rev.root_sha256 == live_rev.root_sha256:
            raise Refused("these changes leave the files exactly as they are live", 422, code="no_change")
    return rev


def _publish_now(project: AgentProject, rev: ProjectFilesRevision, by) -> ProjectFilesRevision:
    previous = live(project)
    if previous is not None and previous.pk != rev.pk:
        previous.status = SUPERSEDED
        previous.save(update_fields=["status"])
    rev.status = PUBLISHED
    rev.published_by = by if getattr(by, "pk", None) else None
    rev.published_at = timezone.now()
    rev.save(update_fields=["status", "published_by", "published_at"])
    git.set_ref(project.pk, git.MAIN, rev.commit, model=ProjectGitRef, owner=OWNER)
    git.set_ref(project.pk, git.tag_ref(rev.number), rev.commit, model=ProjectGitRef, owner=OWNER)
    git.delete_ref(project.pk, git.DRAFT, model=ProjectGitRef, owner=OWNER)
    return rev


def publish(project: AgentProject, number: int, *, by, expect_root: str = "") -> ProjectFilesRevision:
    """Draft `number` goes live; the old live revision is superseded. The route decides who
    may (`api_project_files`)."""
    with transaction.atomic():
        project = _locked(project)
        rev = ProjectFilesRevision.objects.select_for_update().filter(project=project, number=number).first()
        if rev is None:
            raise Refused(f"there is no revision {number}", 404, code="not_found")
        if rev.status != DRAFT:
            raise Refused(f"revision {number} is {rev.status}, not a draft", 409, code="not_draft")
        if expect_root and rev.root_sha256 != expect_root:
            raise Refused(f"revision {number} changed since it was asked for", 409, code="changed")
        live_rev = live(project)
        if rev.parent_id != (live_rev.pk if live_rev is not None else None):
            raise Refused(f"revision {number} was drafted on files that are no longer live", 409, code="stale_base")
        return _publish_now(project, rev, by)


def _discard(rev: ProjectFilesRevision) -> None:
    rev.status = DISCARDED
    rev.save(update_fields=["status"])
    git.delete_ref(rev.project_id, git.DRAFT, model=ProjectGitRef, owner=OWNER)


def discard(project: AgentProject, number: int) -> ProjectFilesRevision:
    with transaction.atomic():
        project = _locked(project)
        rev = revision(project, number)
        if rev is None:
            raise Refused(f"there is no revision {number}", 404, code="not_found")
        if rev.status != DRAFT:
            raise Refused(f"revision {number} is {rev.status}, not a draft", 409, code="not_draft")
        _discard(rev)
    return rev


def rollback(project: AgentProject, number: int, *, by, message: str = "") -> ProjectFilesRevision:
    """A NEW revision whose files are revision `number`'s, published. History is kept."""
    with transaction.atomic():
        project = _locked(project)
        old = revision(project, number)
        if old is None or old.status not in (PUBLISHED, SUPERSEDED):
            raise Refused(f"revision {number} was never published", 409, code="not_published")
        live_rev = live(project)
        if live_rev is not None and live_rev.pk == old.pk:
            raise Refused(f"revision {number} is already live", 409, code="already_live")
        pending = draft(project)
        if pending is not None:
            _discard(pending)
        rev = _new_revision(project, files_of(old), message=message or f"Roll back to revision {number}",
                            parent=live_rev, by=by)
        return _publish_now(project, rev, by)
