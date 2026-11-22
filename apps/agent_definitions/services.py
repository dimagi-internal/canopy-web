"""A stored agent's definition, changed: create, convert, draft, publish, discard, roll
back, export, detach.

Every revision is a commit in the agent's canopy-hosted git repository
(`apps.agents.definition_git`): a draft is the commit on `refs/heads/draft`; publishing
writes the generated files into a commit on the live one, moves `refs/heads/main` to it
and tags it `r<N>`. Editing a stored definition changes what runs with the agent's vault
and credentials, so:

* every write goes through the file policy (`canopy_sdk.agent_definition.check_tree`): a
  tree never holds hooks, MCP servers, another executable, a symlink, a path outside the
  editable set, or a secret-shaped string;
* the files canopy writes itself (`definition_store.generated_files`) are rewritten on
  every revision and never taken from an upload;
* a draft goes live only by a PERSON who administers the agent: `publish`, `rollback`
  and `convert` are refused to any agent login or turn token (the routes ask
  `harness.acting_person`), and an agent's own draft files an approval request
  (`agent.definition.publish`) its owner and admins decide;
* revisions are append-only: nothing here deletes or edits a published tree;
* once an agent's definition moved to GitHub (`export`, `detach`), its canopy repository
  is a read-only archive: every further write is 409 `exported`.

Doc: `docs/architecture/stored-agents.md`.
"""
from __future__ import annotations

import contextlib
import difflib
import io
import logging

from canopy_agent_factory import AgentSpec
from canopy_sdk import agent_definition as policy
from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.agents import definition_git as git
from apps.agents import definition_store as store
from apps.agents import skill_history
from apps.agents.creation import render
from apps.approvals import services as approvals
from apps.agents.credentials.delegations import repo_full_name
from apps.agents.models import Agent, AgentDefinitionRevision, AgentGitRef

logger = logging.getLogger(__name__)

DRAFT, PUBLISHED, SUPERSEDED, DISCARDED = (AgentDefinitionRevision.DRAFT, AgentDefinitionRevision.PUBLISHED,
                                           AgentDefinitionRevision.SUPERSEDED, AgentDefinitionRevision.DISCARDED)
#: The diff of one file is cut here; a reviewer is told it was.
MAX_DIFF_LINES_PER_FILE = 2000
#: The most a convert may carry, files and history together, in raw bytes: what fits
#: in one request body (`DATA_UPLOAD_MAX_MEMORY_SIZE`) once base64'd.
MAX_CONVERT_BYTES = 1_800_000
#: Where an export pushes: `<base><owner>/<name>.git`.
GITHUB_GIT_BASE = "https://github.com/"


class Refused(Exception):
    """A change canopy will not make. `code` is stable for a client; `problems` lists the
    policy's reasons (`Problem.as_dict()`), when the policy refused."""

    def __init__(self, message: str, status: int = 409, *, code: str = "", problems: list | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.problems = problems or []


def _policy_refusal(problems) -> Refused:
    first = problems[0]
    more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
    where = f"{first.path}: " if first.path else ""
    return Refused(f"the definition breaks the file policy — {where}{first.message}{more}", 422,
                   code="policy", problems=[p.as_dict() for p in problems])


def _editable_only(files: dict[str, tuple[bytes, bool]], slug: str) -> dict[str, tuple[bytes, bool]]:
    return {p: f for p, f in files.items() if p not in policy.generated_paths(slug)}


def _check_edits(files: dict[str, tuple[bytes, bool]], slug: str) -> None:
    problems = policy.check_tree(
        [{"path": p, "content": data, "executable": ex} for p, (data, ex) in files.items()], slug=slug)
    if problems:
        raise _policy_refusal(problems)


def _full_tree(agent: Agent, number: int, editable: dict[str, tuple[bytes, bool]]
               ) -> tuple[dict[str, tuple[bytes, bool]], dict, str]:
    """The whole tree of revision `number`: the editable files (checked) plus the files
    canopy generates (rewritten), checked again as one tree for the caps. Returns the
    files, their manifest (`tree`) and its root."""
    _check_edits(editable, agent.slug)
    full = {**editable, **store.generated_files(agent, number)}
    problems = policy.check_tree(
        [{"path": p, "content": data, "executable": ex} for p, (data, ex) in full.items()],
        slug=agent.slug, allow_generated=True)
    if problems:
        raise _policy_refusal(problems)
    tree = store.tree_of(full)
    return dict(sorted(full.items())), tree, store.root_of(tree)


def _author(agent: Agent, by, by_agent: bool) -> bytes:
    if by_agent or not getattr(by, "pk", None):
        return git.agent_author(agent.slug)
    return git.person_author(by)


def _commit(agent: Agent, files: dict[str, tuple[bytes, bool]], *, parent: str, author: bytes,
            message: str, objects: git.AgentObjectStore | None = None) -> str:
    """Write `files` as a commit on `parent` into the agent's repository; return its sha.
    Moves no ref."""
    objects = objects or store.object_store(agent)
    sha = git.make_commit(objects, tree=git.write_tree(objects, files), parents=[parent] if parent else [],
                          author=author, message=message)
    objects.flush()
    return sha


def _refuse_exported(agent: Agent) -> None:
    if agent.definition_exported_to:
        raise Refused(f"{agent.slug}'s definition moved to github.com/{agent.definition_exported_to}; its "
                      "canopy repository is a read-only archive", 409, code="exported")


def _locked(agent: Agent) -> Agent:
    """The agent row, locked: numbering and the one-draft rule are decided under it."""
    return Agent.objects.select_for_update().get(pk=agent.pk)


def _next_number(agent: Agent) -> int:
    top = AgentDefinitionRevision.objects.filter(agent=agent).aggregate(n=Max("number"))["n"]
    return (top or 0) + 1


def live(agent: Agent) -> AgentDefinitionRevision | None:
    return agent.live_revision


def draft(agent: Agent) -> AgentDefinitionRevision | None:
    return AgentDefinitionRevision.objects.filter(agent=agent, status=DRAFT).first()


def revision(agent: Agent, number: int) -> AgentDefinitionRevision | None:
    return AgentDefinitionRevision.objects.filter(agent=agent, number=number).first()


def _publish_now(agent: Agent, rev: AgentDefinitionRevision, by) -> AgentDefinitionRevision:
    """Make `rev` live: `refs/heads/main` and `refs/tags/r<N>` name its commit, and the
    draft ref goes. The caller holds the agent's lock."""
    previous = agent.live_revision
    if previous is not None and previous.pk != rev.pk:
        previous.status = SUPERSEDED
        previous.save(update_fields=["status"])
    rev.status = PUBLISHED
    rev.published_by = by if getattr(by, "pk", None) else None
    rev.published_at = timezone.now()
    rev.save(update_fields=["status", "published_by", "published_at", "tree", "root_sha256", "commit"])
    git.set_ref(agent.pk, git.MAIN, rev.commit)
    git.set_ref(agent.pk, git.tag_ref(rev.number), rev.commit)
    git.delete_ref(agent.pk, git.DRAFT)
    agent.live_revision = rev
    agent.save(update_fields=["live_revision", "updated_at"])
    store.refresh_skill_catalog(agent)
    transaction.on_commit(lambda: _after_publish(agent.pk))
    return rev


def _after_publish(agent_pk: int) -> None:
    agent = Agent.objects.filter(pk=agent_pk).first()
    if agent is None:
        return
    try:
        skill_history.sync(agent, force=True)
    except Exception:  # noqa: BLE001 — a history refresh must not undo a publish
        logger.exception("skill history for stored agent %s failed", agent.slug)


def _new_revision(agent: Agent, *, editable: dict[str, tuple[bytes, bool]], message: str, status: str,
                  parent: AgentDefinitionRevision | None, by, by_agent: bool = False,
                  turn=None, parent_commit: str = "") -> AgentDefinitionRevision:
    """A new revision, committed on `parent`'s commit (or `parent_commit`, for a first
    revision with history behind it): live at once when `status` is published, else the
    agent's draft (`refs/heads/draft`)."""
    number = _next_number(agent)
    files, tree, root = _full_tree(agent, number, editable)
    message = (message or "").strip()[:4000]
    commit = _commit(agent, files, parent=parent.commit if parent is not None else parent_commit,
                     author=_author(agent, by, by_agent), message=message or f"Revision {number}")
    rev = AgentDefinitionRevision.objects.create(
        agent=agent, number=number, parent=parent, tree=tree, root_sha256=root, commit=commit,
        message=message, author_user=by if getattr(by, "pk", None) else None,
        author_agent_turn=turn, by_agent=by_agent, status=DRAFT)
    if status == PUBLISHED:
        _publish_now(agent, rev, by)
    else:
        git.set_ref(agent.pk, git.DRAFT, commit)
    return rev


# --- creating a stored agent, and converting a repo agent -----------------


def factory_files(agent: Agent, *, mandate: str, stakeholders: str = "", author) -> dict[str, tuple[bytes, bool]]:
    """The agent factory's scaffold, kept to what a stored agent may hold: the files a
    repo agent needs for GitHub (workflows, the version-bump script, the Python test,
    `bin/_env.py`) are dropped, and canopy writes its own generated files."""
    with contextlib.redirect_stderr(io.StringIO()):  # the factory warns when there is no mailbox
        rendered = render(AgentSpec(
            slug=agent.slug, display_name=agent.name, mandate=mandate, mailbox=agent.email or "",
            stakeholders=stakeholders, author_name=(author.get_full_name() or author.email) if author else "canopy",
            author_email=(author.email if author else store.CANOPY_EMAIL)))
    return {path: (content.encode("utf-8"), False) for path, (content, _) in rendered.items()
            if policy.editable(path)}


def create_stored_agent(*, slug: str, name: str, mandate: str, stakeholders: str, workspace, owner,
                        by) -> Agent:
    """A new agent whose definition is the factory's scaffold, as revision 1, published."""
    with transaction.atomic():
        agent = Agent.objects.create(slug=slug, name=name, description=mandate[:2000], workspace=workspace,
                                     owner=owner, source=Agent.STORED, repo_url="", repo_ref="")
        files = factory_files(agent, mandate=mandate, stakeholders=stakeholders, author=owner or by)
        agent = _locked(agent)
        _new_revision(agent, editable=files, message="Created from the canopy agent factory",
                      status=PUBLISHED, parent=None, by=by)
        agent.refresh_from_db()
    return agent


def convert(agent: Agent, *, files: dict[str, tuple[bytes, bool]], message: str, by,
            bundle: bytes | None = None) -> AgentDefinitionRevision:
    """Turn a repo agent into a stored one: `files` (the repo's tracked files) become
    revision 1, published. Generated paths in the upload are ignored — canopy rewrites
    them — and anything else outside the policy refuses the whole conversion.

    `bundle` (a `git bundle` of the repo's branch) keeps its history: every object goes
    into the agent's repository under `refs/heads/imported` — after every blob in it,
    in every commit, passed the secret scan — and revision 1 is a commit on its tip."""
    size = sum(len(d) for d, _ in files.values()) + len(bundle or b"")
    if size > MAX_CONVERT_BYTES:
        raise Refused(f"the conversion is {size // 1024} KB, more than the {MAX_CONVERT_BYTES // 1024} KB one "
                      "request carries" + (" — its history is too large to import: convert its files only, "
                                            "without --with-history" if bundle else ""), 413, code="too_large")
    history = None
    if bundle:
        try:
            history = git.read_bundle(bundle)
        except git.GitError as exc:
            raise Refused(str(exc), exc.status, code=exc.code, problems=exc.problems) from None
    with transaction.atomic():
        agent = _locked(agent)
        if agent.is_stored:
            raise Refused(f"{agent.slug} is already a stored agent", 409, code="already_stored")
        _refuse_exported(agent)
        if AgentGitRef.objects.filter(agent=agent).exists():
            raise Refused(f"canopy already keeps a repository for {agent.slug}", 409, code="exported")
        parent_commit = ""
        if history is not None:
            objects = store.object_store(agent)
            for obj in history.objects:
                objects.add_object(obj)
            objects.flush()
            git.set_ref(agent.pk, git.IMPORTED, history.tip)
            parent_commit = history.tip
        rev = _new_revision(agent, editable=_editable_only(files, agent.slug),
                            message=message or "Converted from its repository", status=PUBLISHED,
                            parent=None, by=by, parent_commit=parent_commit)
        agent.source = Agent.STORED
        agent.repo_url = ""
        agent.repo_ref = ""
        agent.save(update_fields=["source", "repo_url", "repo_ref", "updated_at"])
    return rev


# --- drafts ---------------------------------------------------------------


def _apply(base: AgentDefinitionRevision, changes: list[dict]) -> dict[str, tuple[bytes, bool]]:
    files = _editable_only(store.files_of(base), base.agent.slug)
    seen = set()
    for change in changes:
        path = change["path"]
        if path in seen:
            raise Refused(f"{path} is changed twice in one draft", 422, code="duplicate")
        seen.add(path)
        if change.get("content") is None:
            if path not in files:
                raise Refused(f"{path} is not in revision {base.number}, so it cannot be deleted", 422,
                              code="not_found")
            files.pop(path)
            continue
        current = files.get(path)
        executable = change.get("executable")
        if executable is None:
            executable = current[1] if current else False
        files[path] = (change["content"], bool(executable))
    return files


def _withdraw(rev: AgentDefinitionRevision, by) -> None:
    """Close the approval a draft asked for, if whoever is acting may — called while the
    draft is still a draft, so the kind can still say who may withdraw it. One left open
    still cannot publish: the kind checks the draft and its root first."""
    if not rev.approval_id:
        return
    try:
        approvals.cancel(rev.approval_id, by)
    except approvals.ApprovalError:
        pass


def create_draft(agent: Agent, *, base: int, changes: list[dict], message: str, by, by_agent: bool,
                 turn=None) -> AgentDefinitionRevision:
    """Create the agent's one draft, or replace it: `changes` applied to the LIVE revision
    `base`. A base that is not live is `stale_base` (409). An agent's draft (`by_agent`)
    also files an approval request to the agent's owner and admins."""
    if not changes:
        raise Refused("a draft needs at least one change", 422, code="empty")
    with transaction.atomic():
        agent = _locked(agent)
        _refuse_exported(agent)
        if not agent.is_stored or agent.live_revision is None:
            raise Refused(f"{agent.slug} is not a stored agent", 409, code="not_stored")
        live_rev = agent.live_revision
        if base != live_rev.number:
            raise Refused(f"revision {base} is not live; the live revision is {live_rev.number}", 409,
                          code="stale_base")
        old = draft(agent)
        if old is not None:
            _discard(old, by)
        rev = _new_revision(agent, editable=_apply(live_rev, changes), message=message, status=DRAFT,
                            parent=live_rev, by=by, by_agent=by_agent, turn=turn)
        if rev.root_sha256 == live_rev.root_sha256:
            # The transaction is undone, the draft ref with it.
            raise Refused("these changes leave the definition exactly as it is live", 422, code="no_change")
        if by_agent:
            from . import publish_kind  # noqa: PLC0415 — publish_kind imports this module

            rev.approval = publish_kind.request(agent, rev, by=by, reason=message)
            rev.save(update_fields=["approval"])
    return rev


def publish(agent: Agent, number: int, *, by, expect_root: str = "") -> AgentDefinitionRevision:
    """Draft `number` goes live; the old live revision is superseded."""
    with transaction.atomic():
        agent = _locked(agent)
        _refuse_exported(agent)
        rev = AgentDefinitionRevision.objects.select_for_update().filter(agent=agent, number=number).first()
        if rev is None:
            raise Refused(f"there is no revision {number}", 404, code="not_found")
        if rev.status != DRAFT:
            raise Refused(f"revision {number} is {rev.status}, not a draft", 409, code="not_draft")
        if expect_root and rev.root_sha256 != expect_root:
            raise Refused(f"revision {number} changed since it was asked for", 409, code="changed")
        if agent.live_revision_id is not None and rev.parent_id != agent.live_revision_id:
            raise Refused(f"revision {number} was drafted on a revision that is no longer live", 409,
                          code="stale_base")
        # The generated files say who the agent is NOW (its name, its description) and
        # which revision this is: written again at the moment it goes live, into the
        # commit `main` and `r<N>` name. Its parent is the live commit the draft was made
        # on, its author the draft's.
        objects = store.object_store(agent)
        editable = _editable_only(store.files_of(rev, objects), agent.slug)
        files, tree, root = _full_tree(agent, rev.number, editable)
        if root != rev.root_sha256 or not rev.commit:
            drafted = objects[rev.commit.encode("ascii")] if rev.commit else None
            live_commit = agent.live_revision.commit if agent.live_revision_id else ""
            rev.commit = _commit(agent, files, parent=live_commit, objects=objects,
                                 author=drafted.author if drafted is not None else _author(agent, by, False),
                                 message=rev.message or f"Revision {rev.number}")
        rev.tree, rev.root_sha256 = tree, root
        return _publish_now(agent, rev, by)


def discard(agent: Agent, number: int, *, by) -> AgentDefinitionRevision:
    with transaction.atomic():
        agent = _locked(agent)
        _refuse_exported(agent)
        rev = AgentDefinitionRevision.objects.filter(agent=agent, number=number).first()
        if rev is None:
            raise Refused(f"there is no revision {number}", 404, code="not_found")
        if rev.status != DRAFT:
            raise Refused(f"revision {number} is {rev.status}, not a draft", 409, code="not_draft")
        _discard(rev, by)
    return rev


def _discard(rev: AgentDefinitionRevision, by) -> None:
    """The draft is kept, marked discarded; its approval is withdrawn and its ref goes."""
    _withdraw(rev, by)
    rev.status = DISCARDED
    rev.save(update_fields=["status"])
    git.delete_ref(rev.agent_id, git.DRAFT)


def rollback(agent: Agent, number: int, *, by, message: str = "") -> AgentDefinitionRevision:
    """A NEW revision whose files are revision `number`'s, published. History is kept."""
    with transaction.atomic():
        agent = _locked(agent)
        _refuse_exported(agent)
        if not agent.is_stored or agent.live_revision is None:
            raise Refused(f"{agent.slug} is not a stored agent", 409, code="not_stored")
        old = AgentDefinitionRevision.objects.filter(agent=agent, number=number).first()
        if old is None or old.status not in (PUBLISHED, SUPERSEDED):
            raise Refused(f"revision {number} was never published", 409, code="not_published")
        pending = draft(agent)
        if pending is not None:
            _discard(pending, by)
        return _new_revision(agent, editable=_editable_only(store.files_of(old), agent.slug),
                             message=message or f"Roll back to revision {number}", status=PUBLISHED,
                             parent=agent.live_revision, by=by)


# --- moving to GitHub ---------------------------------------------------------


def _moved(agent: Agent, repo: str, ref: str, by) -> None:
    """The agent now runs from github.com/`repo` @ `ref`; canopy's repository stays as a
    read-only archive. The caller holds the agent's lock."""
    pending = draft(agent)
    if pending is not None:
        _discard(pending, by)
    agent.source = Agent.REPO
    agent.repo_url = f"https://github.com/{repo}"
    agent.repo_ref = ref
    agent.live_revision = None
    agent.definition_exported_to = repo
    agent.save(update_fields=["source", "repo_url", "repo_ref", "live_revision", "definition_exported_to",
                              "updated_at"])


def _movable(agent: Agent) -> None:
    _refuse_exported(agent)
    if not agent.is_stored or agent.live_revision is None:
        raise Refused(f"{agent.slug} is not a stored agent", 409, code="not_stored")


def _redacted(text: str, secret: str) -> str:
    """`text` (an error from git) with the token, and anything token-shaped, blanked."""
    from apps.common import redact  # noqa: PLC0415 — only a failed export pays for it

    if secret:
        text = text.replace(secret, "***")
    return redact.redact_text(text)[0]


def export(agent: Agent, *, repo: str, github_token: str, by, ref: str = "main") -> dict:
    """Push the agent's repository — `main`, all its history, and every `r<N>` tag — to
    the EMPTY GitHub repository `repo` (`owner/name`) with `github_token`, then switch the
    agent to `source=repo` there. The token is used for this push only: it is not kept,
    logged or returned. 409 `not_empty` when the repository has any ref; 502 when GitHub
    cannot be reached or refuses the push."""
    full = repo_full_name(f"https://github.com/{(repo or '').strip().strip('/')}")
    if not full:
        raise Refused(f"{repo!r} is not a GitHub repository (owner/name)", 422, code="bad_repo")
    ref = (ref or "main").strip()
    if not ref or "/" in ref or ref.startswith("-"):
        raise Refused(f"{ref!r} is not a branch name", 422, code="bad_ref")
    token = (github_token or "").strip()
    if not token:
        raise Refused("export needs `github_token`: a GitHub token of yours that may push to the repository, used "
                      "for this push only. canopy cannot open the agent's delegated GitHub token, which is sealed to "
                      "the machines that run it. Or push it yourself (`--push-myself`) and detach.", 422,
                      code="no_token")
    base = getattr(settings, "CANOPY_GITHUB_GIT_BASE", "") or GITHUB_GIT_BASE
    url = f"{base}{full}.git"
    auth = {"username": "x-access-token", "password": token} if url.startswith("http") else {}
    with transaction.atomic():
        agent = _locked(agent)
        _movable(agent)
        refs = git.refs_of(agent.pk)
        objects = store.object_store(agent)
        try:
            remote = git.remote_refs(url, **auth)
        except Exception as exc:  # noqa: BLE001 — dulwich raises a kind per transport
            raise Refused(f"github.com/{full} could not be read ({_redacted(str(exc), token)[:300]}); it must "
                          "exist, be empty, and the token must reach it", 502, code="unreachable") from None
        if remote:
            raise Refused(f"github.com/{full} is not empty; export needs an empty repository", 409,
                          code="not_empty")
        updates = {f"refs/heads/{ref}": refs[git.MAIN],
                   **{name: sha for name, sha in refs.items() if name.startswith(git.TAG_PREFIX)}}
        try:
            git.push(objects, url, updates, **auth)
        except git.GitError as exc:
            raise Refused(_redacted(str(exc), token), exc.status, code=exc.code) from None
        except Exception as exc:  # noqa: BLE001
            raise Refused(f"the push to github.com/{full} failed ({_redacted(str(exc), token)[:300]})", 502,
                          code="push_failed") from None
        live = agent.live_revision
        _moved(agent, full, ref, by)
    return {"repo": full, "repo_url": agent.repo_url, "repo_ref": ref, "commit": updates[f"refs/heads/{ref}"],
            "revision": live.number, "tags": sorted(n for n in updates if n.startswith(git.TAG_PREFIX))}


def detach(agent: Agent, *, repo_url: str, by, ref: str = "main") -> dict:
    """The agent's definition moved to GitHub by a person's own push (`canopy agent
    definition export --push-myself`): the same switch as `export`, with nothing pushed."""
    full = repo_full_name(repo_url or "")
    if not full:
        raise Refused(f"{repo_url!r} is not a GitHub repository URL", 422, code="bad_repo")
    ref = (ref or "main").strip()
    if not ref or "/" in ref or ref.startswith("-"):
        raise Refused(f"{ref!r} is not a branch name", 422, code="bad_ref")
    with transaction.atomic():
        agent = _locked(agent)
        _movable(agent)
        live = agent.live_revision
        _moved(agent, full, ref, by)
    return {"repo": full, "repo_url": agent.repo_url, "repo_ref": ref, "commit": live.commit,
            "revision": live.number, "tags": []}


# --- reading ----------------------------------------------------------------


def diff(a: AgentDefinitionRevision, b: AgentDefinitionRevision) -> list[dict]:
    """One entry per path that differs from `b` to `a` (b is the "before"): its status
    (`added`, `deleted`, `modified`, `mode`) and a unified diff of its text."""
    before, after = b.tree or {}, a.tree or {}
    objects = store.object_store(a.agent_id)
    old_files = store.files_of(b, objects) if b.commit else {}
    new_files = store.files_of(a, objects) if a.commit else {}
    out = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        status = "added" if old is None else "deleted" if new is None else (
            "modified" if old["sha256"] != new["sha256"] else "mode")
        old_text = old_files.get(path, (b"", False))[0].decode("utf-8", errors="replace") if old else ""
        new_text = new_files.get(path, (b"", False))[0].decode("utf-8", errors="replace") if new else ""
        lines = list(difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=f"a/{path}" if old else "/dev/null", tofile=f"b/{path}" if new else "/dev/null"))
        truncated = len(lines) > MAX_DIFF_LINES_PER_FILE
        out.append({"path": path, "status": status, "diff": "".join(lines[:MAX_DIFF_LINES_PER_FILE]),
                    "truncated": truncated})
    return out
