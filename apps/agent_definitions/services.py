"""A stored agent's definition, changed: create, convert, draft, publish, discard, roll back.

Editing a stored definition changes what runs with the agent's vault and credentials, so:

* every write goes through the file policy (`canopy_sdk.agent_definition.check_tree`): a
  tree never holds hooks, MCP servers, another executable, a symlink, a path outside the
  editable set, or a secret-shaped string;
* the files canopy writes itself (`definition_store.generated_files`) are rewritten on
  every revision and never taken from an upload;
* a draft goes live only by a PERSON who administers the agent: `publish`, `rollback`
  and `convert` are refused to any agent login or turn token (the routes ask
  `harness.acting_person`), and an agent's own draft files an approval request
  (`agent.definition.publish`) its owner and admins decide;
* revisions are append-only: nothing here deletes or edits a published tree.

Doc: `docs/architecture/stored-agents.md`.
"""
from __future__ import annotations

import contextlib
import difflib
import io
import logging

from canopy_agent_factory import AgentSpec
from canopy_sdk import agent_definition as policy
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.agents import definition_store as store
from apps.agents import skill_history
from apps.agents.creation import render
from apps.approvals import services as approvals
from apps.agents.models import Agent, AgentDefinitionRevision

logger = logging.getLogger(__name__)

DRAFT, PUBLISHED, SUPERSEDED, DISCARDED = (AgentDefinitionRevision.DRAFT, AgentDefinitionRevision.PUBLISHED,
                                           AgentDefinitionRevision.SUPERSEDED, AgentDefinitionRevision.DISCARDED)
#: The diff of one file is cut here; a reviewer is told it was.
MAX_DIFF_LINES_PER_FILE = 2000


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


def _full_tree(agent: Agent, number: int, editable: dict[str, tuple[bytes, bool]]) -> tuple[dict, str]:
    """The whole tree of revision `number`: the editable files (checked) plus the files
    canopy generates (rewritten), checked again as one tree for the caps."""
    _check_edits(editable, agent.slug)
    full = {**editable, **store.generated_files(agent, number)}
    problems = policy.check_tree(
        [{"path": p, "content": data, "executable": ex} for p, (data, ex) in full.items()],
        slug=agent.slug, allow_generated=True)
    if problems:
        raise _policy_refusal(problems)
    tree = store.tree_of(full)
    return tree, store.root_of(tree)


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
    """Make `rev` live. The caller holds the agent's lock."""
    previous = agent.live_revision
    if previous is not None and previous.pk != rev.pk:
        previous.status = SUPERSEDED
        previous.save(update_fields=["status"])
    rev.status = PUBLISHED
    rev.published_by = by if getattr(by, "pk", None) else None
    rev.published_at = timezone.now()
    rev.save(update_fields=["status", "published_by", "published_at", "tree", "root_sha256"])
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
                  turn=None) -> AgentDefinitionRevision:
    number = _next_number(agent)
    tree, root = _full_tree(agent, number, editable)
    rev = AgentDefinitionRevision.objects.create(
        agent=agent, number=number, parent=parent, tree=tree, root_sha256=root,
        message=(message or "").strip()[:4000], author_user=by if getattr(by, "pk", None) else None,
        author_agent_turn=turn, by_agent=by_agent, status=DRAFT)
    if status == PUBLISHED:
        _publish_now(agent, rev, by)
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


def convert(agent: Agent, *, files: dict[str, tuple[bytes, bool]], message: str, by) -> AgentDefinitionRevision:
    """Turn a repo agent into a stored one: `files` (the repo's tracked files) become
    revision 1, published. Generated paths in the upload are ignored — canopy rewrites
    them — and anything else outside the policy refuses the whole conversion."""
    with transaction.atomic():
        agent = _locked(agent)
        if agent.is_stored:
            raise Refused(f"{agent.slug} is already a stored agent", 409, code="already_stored")
        rev = _new_revision(agent, editable=_editable_only(files, agent.slug),
                            message=message or "Converted from its repository", status=PUBLISHED,
                            parent=None, by=by)
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
        if not agent.is_stored or agent.live_revision is None:
            raise Refused(f"{agent.slug} is not a stored agent", 409, code="not_stored")
        live_rev = agent.live_revision
        if base != live_rev.number:
            raise Refused(f"revision {base} is not live; the live revision is {live_rev.number}", 409,
                          code="stale_base")
        old = draft(agent)
        if old is not None:
            _withdraw(old, by)
            old.status = DISCARDED
            old.save(update_fields=["status"])
        rev = _new_revision(agent, editable=_apply(live_rev, changes), message=message, status=DRAFT,
                            parent=live_rev, by=by, by_agent=by_agent, turn=turn)
        if rev.root_sha256 == live_rev.root_sha256:
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
        # which revision this is: written again at the moment it goes live.
        editable = _editable_only(store.files_of(rev), agent.slug)
        rev.tree, rev.root_sha256 = _full_tree(agent, rev.number, editable)
        return _publish_now(agent, rev, by)


def discard(agent: Agent, number: int, *, by) -> AgentDefinitionRevision:
    with transaction.atomic():
        agent = _locked(agent)
        rev = AgentDefinitionRevision.objects.filter(agent=agent, number=number).first()
        if rev is None:
            raise Refused(f"there is no revision {number}", 404, code="not_found")
        if rev.status != DRAFT:
            raise Refused(f"revision {number} is {rev.status}, not a draft", 409, code="not_draft")
        _withdraw(rev, by)
        rev.status = DISCARDED
        rev.save(update_fields=["status"])
    return rev


def rollback(agent: Agent, number: int, *, by, message: str = "") -> AgentDefinitionRevision:
    """A NEW revision whose files are revision `number`'s, published. History is kept."""
    with transaction.atomic():
        agent = _locked(agent)
        if not agent.is_stored or agent.live_revision is None:
            raise Refused(f"{agent.slug} is not a stored agent", 409, code="not_stored")
        old = AgentDefinitionRevision.objects.filter(agent=agent, number=number).first()
        if old is None or old.status not in (PUBLISHED, SUPERSEDED):
            raise Refused(f"revision {number} was never published", 409, code="not_published")
        pending = draft(agent)
        if pending is not None:
            _withdraw(pending, by)
            pending.status = DISCARDED
            pending.save(update_fields=["status"])
        return _new_revision(agent, editable=_editable_only(store.files_of(old), agent.slug),
                             message=message or f"Roll back to revision {number}", status=PUBLISHED,
                             parent=agent.live_revision, by=by)


# --- reading ----------------------------------------------------------------


def diff(a: AgentDefinitionRevision, b: AgentDefinitionRevision) -> list[dict]:
    """One entry per path that differs from `b` to `a` (b is the "before"): its status
    (`added`, `deleted`, `modified`, `mode`) and a unified diff of its text."""
    before, after = b.tree or {}, a.tree or {}
    out = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        status = "added" if old is None else "deleted" if new is None else (
            "modified" if old["sha256"] != new["sha256"] else "mode")
        old_text = store.read_blob(old["sha256"]).decode("utf-8", errors="replace") if old else ""
        new_text = store.read_blob(new["sha256"]).decode("utf-8", errors="replace") if new else ""
        lines = list(difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=f"a/{path}" if old else "/dev/null", tofile=f"b/{path}" if new else "/dev/null"))
        truncated = len(lines) > MAX_DIFF_LINES_PER_FILE
        out.append({"path": path, "status": status, "diff": "".join(lines[:MAX_DIFF_LINES_PER_FILE]),
                    "truncated": truncated})
    return out
