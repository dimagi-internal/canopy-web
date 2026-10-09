"""What a session DID, aggregated as its transcript streams in: `Session.activity`.

Answers "which sessions built X?" without anyone re-reading local `.jsonl`
transcripts. Before this, selecting every session that worked on connect-labs'
`connect_labs/supply_chain` meant rebuilding the signal from Claude transcripts on
the laptop that happened to hold them (board task T75) — the server held the rows
but had no index over what they did.

The shape (all keys optional; an empty dict is a session nothing is known about)::

    {
      "cwd":      "/Users/x/emdash/worktrees/hal-8ac55e86/emdash-…",  # latest
      "cwds":     [...],                       # every distinct cwd seen
      "repos":    ["hal", "connect-labs"],     # short repo names, lowercased
      "remotes":  ["dimagi-internal/connect-labs"],   # owner/name, lowercased
      "branches": ["main", "hal/t75-…"],      # every branch seen, in order
      "prs":      [{"repo": "dimagi-internal/connect-labs", "number": 12,
                    "url": "https://github.com/…/pull/12",
                    "actions": ["created", "merge"]}],
      "paths":    {"connect-labs:connect_labs/supply_chain": 14},   # edits per dir
      "mcp_tools": {"mcp__connect_labs__pipeline_sql": 3},           # calls per tool
      "pending":  {...}    # internal: tool calls awaiting their result; not served
    }

Where each signal comes from — the limits are part of the contract:

- `cwd` / `branches`: the runner sends each row's record-level `cwd` and
  `gitBranch` (Claude Code stamps both on every record), so a long session that
  opens many branches records all of them, not just its starting one. Rows from a
  runner that predates this carry neither; a branch then only appears if a Bash
  command created it (`git checkout -b`, `git switch -c`, `worktree add -b`) or a
  push printed it.
- `repos`: from emdash path conventions (`~/emdash/worktrees/<repo>[-<hash>]/…`,
  `~/emdash/repositories/<repo>/…`) found in the cwd, in edited file paths, and in
  `cd` / `git -C` targets; plus the name half of every remote.
- `remotes`: GitHub `owner/name` from `gh … -R/--repo`, PR URLs, and `git push`
  output. Not a `git remote` lookup — the server cannot run git on the runner's
  disk — so a session that never pushed or touched a PR has repos but no remotes.
- `prs`: `gh pr create` → "created", from the URL it prints. `gh pr merge` that did
  not error → "merge" — which on a merge-queue repo means ENQUEUED or auto-merge
  ARMED, not necessarily merged; the PR's own state is the authority for that.
- `paths`: Edit / Write / MultiEdit / NotebookEdit targets, as `<repo>:<first two
  directories>` relative to the repo root (or the cwd, or `~`).
- `mcp_tools`: every `mcp__*` tool call, counted.

Counts are folded from NEW rows only (`persist_transcript_rows` passes the rows it
actually inserted), so the runner's routine re-ships never double-count. The few
paths that DELETE derived rows to re-derive them call `rebuild`, which refolds
the counters from what is left. The set-like keys are unions and survive a
rebuild untouched — which is what keeps the runner-only context (cwd, gitBranch,
never stored per row) from being lost when rows are re-derived.
"""
from __future__ import annotations

import copy
import json
import re
import shlex

#: Caps, so a pathological session cannot grow its row without bound. Generous
#: next to anything observed; a session past them keeps its first N, which is
#: the honest failure for a "what did it touch" index.
MAX_VALUES = 40      # each list: cwds, repos, remotes, branches
MAX_PRS = 60
MAX_COUNTED = 80     # distinct keys in paths / mcp_tools
MAX_PENDING = 50

_LISTS = ("cwds", "repos", "remotes", "branches")
_COUNTERS = ("paths", "mcp_tools")
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

_SLUG = r"[\w.-]+/[\w.-]+"
_PR_URL = re.compile(
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+)/pull/(?P<num>\d+)\b")
# Only gh's own "… pull request owner/name#N" line — a bare `x/y#N` anywhere in
# the output (help text, a quoted example) is not a PR this session touched.
_PR_REF = re.compile(rf"\bpull request (?P<slug>{_SLUG})#(?P<num>\d+)\b")
_PUSH_TO = re.compile(
    r"^To (?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?\s*$", re.M)
_PUSH_NEW = re.compile(
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+)/pull/new/(?P<branch>\S+)")
_PUSH_BRANCH = re.compile(r"\*\s+\[new branch\]\s+\S+\s+->\s+(?P<branch>\S+)")
# `-R owner/name` as a WHOLE token: `-R dimagi-internal/$r` is a variable, not a
# repo, and the lookahead refuses the `dimagi-internal/` prefix it would leave.
# Read only from a `gh` invocation — `cp -R skills/x`, `grep -R a/b` are paths.
_GH_REPO_FLAG = re.compile(
    rf"(?:^|\s)(?:-R|--repo)(?:\s+|=)['\"]?(?P<slug>{_SLUG})(?=$|[\s'\";&|)])")
_GH_CALL = re.compile(r"(?:^|[\s(])gh\s")
#: A repo or owner name as GitHub allows it. Anything else — `$r`, a glob, a
#: backtick — is an unexpanded shell token, not a repo.
_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_BRANCH_BAD = re.compile(r"[$`*?\[\]{}()<>|;&\\\s~^:]")
#: Repos the fleet works in, for mapping a scratch-worktree folder
#: (`canopy-web-<topic>`, `connect-labs-<topic>`) back to its repo when the
#: session's own remotes do not name it. Longest prefix wins.
KNOWN_REPOS = (
    "canopy-web", "canopy", "connect-labs", "ace-web", "ace", "hal", "eva", "ada", "echo",
    "chrome-sales", "commcare-connect", "commcare-hq", "commcare-nova", "nova-plugin",
    "open-chat-studio", "scout",
)
#: Worktree-folder shorthands seen in the fleet: `cw-<topic>` is canopy-web, `cl-` connect-labs.
REPO_ALIASES = {"cw": "canopy-web", "cl": "connect-labs"}
_NEW_BRANCH = re.compile(
    r"\bgit\s+(?:-C\s+\S+\s+)?(?:(?:checkout|switch)\s+(?:\S+\s+)*?-[bBcC]"
    r"|worktree\s+add\b(?:\s+\S+)*?\s+-[bB])\s+(?P<branch>[^\s;&|)]+)")
_CD_TARGET = re.compile(r"(?:\bcd|\bpushd|\bgit\s+-C|--repo-path)\s+(?P<path>[^\s;&|)]+)")
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\n|\|")

# emdash's layouts (canopy_transcript.paths.parse_emdash_worktree, run as a regex
# because the server does not know the runner's $HOME): `<repo>[-<8 hex>]/
# [emdash/]<task>/…` under worktrees, `<repo>/…` under repositories.
_WORKTREE = re.compile(
    r"/emdash/worktrees/(?P<repo>[^/]+?)(?:-[0-9a-f]{8})?/(?:emdash/)?[^/]+(?:/(?P<rest>.*))?$")
_REPOSITORY = re.compile(r"/(?:emdash/repositories|emdash-projects)/(?P<repo>[^/]+)(?:/(?P<rest>.*))?$")
# A `mktemp -d` scratch worktree (macOS `$TMPDIR/tmp.XXXX`, Linux `/tmp/tmp.XXXX`).
_SCRATCH = re.compile(r"/tmp\.[A-Za-z0-9]+(?:/(?P<rest>.*))?$")
_HOME = re.compile(r"^(?:/(?:Users|home)/[^/]+|~)(?=/|$)")


def public(data: dict | None) -> dict:
    """The served view: everything but the internal pending-call bookkeeping."""
    return {k: v for k, v in (data or {}).items() if k != "pending"}


def locate(path: str) -> tuple[str, str | None]:
    """(repo, path relative to that repo's root) for a path under an emdash
    checkout; ("", rel) for a scratch worktree whose repo is unknown; ("", None)
    when nothing is recognised."""
    for rx in (_WORKTREE, _REPOSITORY):
        m = rx.search(path)
        if m:
            return m.group("repo").lower(), m.group("rest") or ""
    m = _SCRATCH.search(path)
    if m:
        return "", m.group("rest") or ""
    return "", None


def _dir_prefix(rel: str) -> str:
    """The first two DIRECTORIES of a relative file path ("." for a top-level file)."""
    parts = [p for p in rel.split("/") if p][:-1]
    return "/".join(parts[:2]) or "."


class Activity:
    """A mutable view of one session's `activity`, folded row by row."""

    def __init__(self, data: dict | None = None):
        self._before = json.dumps(data or {}, sort_keys=True, default=str)
        self.d: dict = copy.deepcopy(data) if isinstance(data, dict) else {}

    @property
    def changed(self) -> bool:
        return json.dumps(self.d, sort_keys=True, default=str) != self._before

    # --- primitives -------------------------------------------------------
    def _add(self, key: str, value) -> None:
        value = (value or "").strip() if isinstance(value, str) else value
        if not value:
            return
        lst = self.d.setdefault(key, [])
        if value not in lst and len(lst) < MAX_VALUES:
            lst.append(value)

    def _count(self, key: str, name: str) -> None:
        counts = self.d.setdefault(key, {})
        if name in counts or len(counts) < MAX_COUNTED:
            counts[name] = counts.get(name, 0) + 1

    def _repo(self, name: str) -> None:
        name = (name or "").lower()
        if _NAME_OK.match(name):
            self._add("repos", name)

    def _remote(self, owner: str, name: str) -> str:
        slug = f"{owner}/{name}".lower()
        if slug.endswith(".git"):
            slug = slug[:-4]
        if not _valid_slug(slug):
            return ""
        self._add("remotes", slug)
        self._repo(slug.split("/", 1)[1])
        return slug

    def _branch(self, branch: str) -> None:
        branch = (branch or "").strip().strip("'\"")
        if branch and branch != "HEAD" and not _BRANCH_BAD.search(branch):
            self._add("branches", branch)

    def _pr(self, repo: str, number: int, url: str, action: str) -> None:
        prs = self.d.setdefault("prs", [])
        for pr in prs:
            if pr.get("number") == number and (pr.get("repo") == repo or not pr.get("repo") or not repo):
                if repo and not pr.get("repo"):
                    pr["repo"] = repo
                if url and not pr.get("url"):
                    pr["url"] = url
                if action not in pr.setdefault("actions", []):
                    pr["actions"].append(action)
                return
        if len(prs) < MAX_PRS:
            prs.append({"repo": repo, "number": number, "url": url, "actions": [action]})

    def _remote_for_repo(self, name: str) -> str:
        """The one known `owner/name` whose name is `name`, else ""."""
        hits = [s for s in self.d.get("remotes", []) if s.split("/", 1)[-1] == (name or "").lower()]
        return hits[0] if len(hits) == 1 else ""

    # --- inputs -----------------------------------------------------------
    def context(self, cwd: str | None, branch: str | None) -> None:
        """A row's record-level `cwd` and `gitBranch`. Idempotent (set union), so
        it is safe to apply to every shipped row, re-ships included."""
        if isinstance(cwd, str) and cwd.strip():
            cwd = cwd.strip()
            self._add("cwds", cwd)
            self.d["cwd"] = cwd
            self._repo(locate(cwd)[0])
        if isinstance(branch, str):
            self._branch(branch)

    def row(self, role: str, text: str, content: dict | None) -> None:
        """Fold one NEW transcript row (a re-ship must not reach here)."""
        content = content if isinstance(content, dict) else {}
        if role == "tool_use":
            self._tool_use(content)
        elif role == "tool_result":
            body = text or (content.get("content") if isinstance(content.get("content"), str) else "")
            self._tool_result(content, body or "")

    def reset_counters(self) -> None:
        for key in (*_COUNTERS, "pending"):
            self.d.pop(key, None)

    # --- tool calls -------------------------------------------------------
    def _tool_use(self, content: dict) -> None:
        name = str(content.get("name") or "")
        tool_input = content.get("input") if isinstance(content.get("input"), dict) else {}
        if name.startswith("mcp__"):
            self._count("mcp_tools", name)
        elif name in _EDIT_TOOLS:
            path = tool_input.get("file_path") or tool_input.get("notebook_path")
            if isinstance(path, str) and path:
                self._edited(path)
        elif name == "Bash":
            command = tool_input.get("command")
            if isinstance(command, str) and command:
                self._bash(str(content.get("id") or ""), command)

    def _edited(self, path: str) -> None:
        repo, rel = locate(path)
        if rel is None:
            cwd = self.d.get("cwd") or ""
            if cwd and path.startswith(cwd.rstrip("/") + "/"):
                repo, rel = locate(cwd)[0], path[len(cwd.rstrip("/")) + 1:]
            else:
                rel = _HOME.sub("~", path).lstrip("/")
        self._repo(repo)
        self._count("paths", f"{repo}:{_dir_prefix(rel)}" if repo else _dir_prefix(rel))

    def _bash(self, call_id: str, command: str) -> None:
        for segment in _SEGMENT_SPLIT.split(command):
            if _GH_CALL.search(segment):
                for m in _GH_REPO_FLAG.finditer(segment):
                    self._remote(*m.group("slug").split("/", 1))
        for m in _NEW_BRANCH.finditer(command):
            self._branch(m.group("branch"))
        for m in _CD_TARGET.finditer(command):
            self._repo(locate(m.group("path").strip("'\""))[0])
        ops: dict = {}
        for segment in _SEGMENT_SPLIT.split(command):
            segment = segment.strip()
            if re.search(r"\bgit\s+(?:-C\s+\S+\s+)?push\b", segment):
                ops["push"] = True
            pr = re.search(r"\bgh\s+pr\s+(create|merge)\b(.*)$", segment)
            if not pr:
                continue
            flag = _GH_REPO_FLAG.search(segment)
            repo = flag.group("slug").lower() if flag else ""
            repo = repo if _valid_slug(repo) else ""
            if pr.group(1) == "create":
                ops["create"] = {"repo": repo}
            else:
                ops["merge"] = {"repo": repo, **_merge_selector(pr.group(2))}
        if ops and call_id:
            pending = self.d.setdefault("pending", {})
            pending[call_id] = ops
            while len(pending) > MAX_PENDING:
                pending.pop(next(iter(pending)))

    def _tool_result(self, content: dict, body: str) -> None:
        call_id = str(content.get("tool_use_id") or "")
        pending = self.d.get("pending") or {}
        ops = pending.pop(call_id, None) if call_id else None
        if not pending:
            self.d.pop("pending", None)
        if not ops:
            return
        failed = bool(content.get("is_error"))
        if ops.get("push"):
            for m in _PUSH_TO.finditer(body):
                self._remote(m.group("owner"), m.group("name"))
            for m in _PUSH_NEW.finditer(body):
                self._remote(m.group("owner"), m.group("name"))
                self._branch(m.group("branch"))
            for m in _PUSH_BRANCH.finditer(body):
                self._branch(m.group("branch"))
        if "create" in ops:
            urls = list(_PR_URL.finditer(body))
            if urls:
                m = urls[-1]
                slug = self._remote(m.group("owner"), m.group("name"))
                self._pr(slug, int(m.group("num")), m.group(0), "created")
        merge = ops.get("merge")
        if merge and not failed:
            number, repo, url = merge.get("number"), merge.get("repo") or "", merge.get("url") or ""
            said = _PR_URL.search(body) or _PR_REF.search(body)
            if said is not None:
                if said.re is _PR_URL:
                    repo = self._remote(said.group("owner"), said.group("name"))
                    url = url or said.group(0)
                else:
                    owner, name = said.group("slug").split("/", 1)
                    repo = self._remote(owner, name)
                number = number or int(said.group("num"))
            if number:
                if repo:
                    self._remote(*repo.split("/", 1))
                else:
                    repo = self._remote_for_repo(self._cwd_repo())
                self._pr(repo, int(number), url, "merge")

    def _cwd_repo(self) -> str:
        return locate(self.d.get("cwd") or "")[0]


#: `gh pr merge` flags that consume the next token, so it is not the selector.
_MERGE_VALUE_FLAGS = {"-R", "--repo", "-b", "--body", "-F", "--body-file", "-t", "--subject",
                      "-A", "--author-email", "--match-head-commit"}


def _merge_selector(args: str) -> dict:
    """`gh pr merge`'s PR argument as {"number"[, "repo", "url"]} — {} when it is a
    branch name or absent (gh then resolves the current branch, which we cannot)."""
    try:
        tokens = shlex.split(args)
    except ValueError:
        tokens = args.split()
    skip = False
    for tok in tokens:
        if skip:
            skip = False
            continue
        if tok in _MERGE_VALUE_FLAGS:
            skip = True
            continue
        if tok.startswith("-"):
            continue
        if tok.lstrip("#").isdigit():
            return {"number": int(tok.lstrip("#"))}
        m = _PR_URL.match(tok)
        if m:
            return {"number": int(m.group("num")), "url": m.group(0),
                    "repo": f"{m.group('owner')}/{m.group('name')}".lower()}
        return {}
    return {}


# --- persistence --------------------------------------------------------------

def fold_rows(act: Activity, rows) -> None:
    """Fold (role, text, content) triples."""
    for role, text, content in rows:
        act.row(role, text or "", content)


#: Keys derived only from Message rows, so a rebuild recomputes them from
#: scratch. That is also how a rebuild sheds values an older extractor got
#: wrong. The runner-only context (cwd, cwds, branches) is not in the rows, so
#: it is kept, cleaned by `normalize`.
_ROW_DERIVED = ("remotes", "repos", "prs")


def rebuild(session, *, save: bool = True) -> Activity:
    """Re-derive everything row-derived from the session's CURRENT Message rows.

    For the paths that delete derived rows to re-derive them (ordinal-scheme
    change, transcript identity change, reset), and for backfilling or
    re-extracting sessions (`rebuild_session_activity`). Counters and the
    row-derived sets are recomputed; cwd/cwds/branches, which only the runner's
    context carries, are kept and the cwds re-yield their repos. Rows already
    removed by retention cannot be refolded, so their PRs and remotes go too."""
    from .models import Message

    act = Activity(session.activity)
    act.reset_counters()
    for key in _ROW_DERIVED:
        act.d.pop(key, None)
    for cwd in list(act.d.get("cwds", [])):
        act._repo(locate(cwd)[0])
    fold_rows(act, Message.objects.filter(session=session).order_by("turn_index")
              .values_list("role", "plaintext", "content").iterator(chunk_size=500))
    # Here as well as in `store`, so a `save=False` caller (the command's
    # --dry-run) sees and counts what WOULD be written. Without it the 2026-10-09
    # prod dry run reported 49 changes against a real run of 150.
    normalize(act.d)
    if save:
        store(session, act)
    return act


def store(session, act: Activity) -> bool:
    """Write `act` onto the session — `activity` and the `activity_keys` derived
    from it, together, so the filter column cannot drift from the data. Only when
    something changed. Returns whether it wrote."""
    normalize(act.d)
    keys = index_keys(act.d)
    if not act.changed and keys == session.activity_keys:
        return False
    session.activity = act.d
    session.activity_keys = keys
    session.save(update_fields=["activity", "activity_keys"])
    return True


def _valid_slug(slug: str) -> bool:
    owner, _, name = (slug or "").partition("/")
    return bool(_NAME_OK.match(owner) and _NAME_OK.match(name))


def canonical_repo(name: str, remote_names: set[str]) -> str:
    """The repo a folder name stands for, or "" to drop it.

    A scratch worktree's folder (`canopy-web-popup-escape`, `cw-fixes`) is named
    after its repo plus a topic, and it was being indexed as a repo of its own —
    so `?repo=connect-labs` missed sessions that only touched
    `connect-labs-today-flake/`, and the list grew a "repo" per topic. Order:
    the session's own remotes (exact, then as a prefix), then the fleet's known
    repos and aliases as a prefix. A name none of those explain is kept only when
    the session has no remotes at all — with remotes, the remote is the better
    answer and an unexplained folder is noise."""
    name = (name or "").lower()
    if not _NAME_OK.match(name):
        return ""
    if name in remote_names or name in KNOWN_REPOS:
        return name
    head = re.split(r"[-_.]", name, maxsplit=1)[0]
    if head in REPO_ALIASES and name != head:
        return REPO_ALIASES[head]
    for candidates in (remote_names, KNOWN_REPOS):
        hits = [c for c in candidates if any(name.startswith(c + sep) for sep in "-_.")]
        if hits:
            return max(hits, key=len)
    return "" if remote_names else name


def normalize(data: dict) -> dict:
    """Clean `data` in place before it is stored: drop shell tokens, map
    worktree folders to their repo (`canonical_repo`), and re-key `paths` to
    match. Idempotent, so applying it on every write is safe."""
    remotes = [r for r in data.get("remotes", []) if _valid_slug(r)]
    if "remotes" in data:
        data["remotes"] = remotes
    names = {r.split("/", 1)[1] for r in remotes}
    if "repos" in data:
        repos: list[str] = []
        for r in data["repos"]:
            c = canonical_repo(r, names)
            if c and c not in repos:
                repos.append(c)
        data["repos"] = repos
    if "paths" in data:
        paths: dict[str, int] = {}
        for key, n in data["paths"].items():
            repo, sep, rest = key.partition(":")
            if sep:
                c = canonical_repo(repo, names)
                key = f"{c}:{rest}" if c else rest
            paths[key] = paths.get(key, 0) + n
        data["paths"] = paths
    if "branches" in data:
        data["branches"] = [b for b in data["branches"]
                            if b and b != "HEAD" and not _BRANCH_BAD.search(b)]
    for pr in data.get("prs", []):
        if pr.get("repo") and not _valid_slug(pr["repo"]):
            pr["repo"] = ""
    return data


# --- list filters -------------------------------------------------------------

def _token(kind: str, value: str) -> str:
    return f"\n{kind}:{value}\n"


def index_keys(data: dict) -> str:
    """The filterable values of `data` as newline-delimited `kind:value` tokens,
    framed by newlines so a `LIKE '%\\nkind:value\\n%'` matches whole values only
    (a git ref cannot contain a newline). PRs index as `pr:<n>` always, plus
    `pr:<owner/name>#<n>` when the repo is known or `pr:?#<n>` when it is not."""
    tokens = [f"repo:{r}" for r in data.get("repos", [])]
    tokens += [f"remote:{r}" for r in data.get("remotes", [])]
    tokens += [f"branch:{b}" for b in data.get("branches", [])]
    for pr in data.get("prs", []):
        n = pr.get("number")
        tokens += [f"pr:{n}", f"pr:{pr.get('repo') or '?'}#{n}"]
    return ("\n" + "\n".join(tokens) + "\n") if tokens else ""


def filter_q(*, repo: str = "", branch: str = "", pr: str = ""):
    """The `?repo=` / `?branch=` / `?pr=` list filters as one Q over
    `activity_keys`. Raises ValueError on a `pr` that names no PR number."""
    from django.db.models import Q

    q = Q()
    if repo:
        value = repo.strip().lower().removesuffix(".git")
        if "/" in value:
            # A full slug also matches the bare name: a session that worked in a
            # checkout but never pushed has the name and no remote.
            q &= (Q(activity_keys__contains=_token("remote", value))
                  | Q(activity_keys__contains=_token("repo", value.split("/", 1)[1])))
        else:
            q &= Q(activity_keys__contains=_token("repo", value)) | Q(project__iexact=value)
    if branch:
        q &= Q(activity_keys__contains=_token("branch", branch.strip()))
    if pr:
        number, slug = parse_pr(pr)
        if slug:
            q &= (Q(activity_keys__contains=_token("pr", f"{slug}#{number}"))
                  | Q(activity_keys__contains=_token("pr", f"?#{number}")))
        else:
            q &= Q(activity_keys__contains=_token("pr", str(number)))
    return q


def parse_pr(value: str) -> tuple[int, str]:
    """(number, "owner/name" or "") from a PR URL, `owner/name#N`, `#N` or `N`."""
    value = value.strip()
    m = _PR_URL.search(value)
    if m:
        return int(m.group("num")), f"{m.group('owner')}/{m.group('name')}".lower()
    m = re.fullmatch(rf"(?P<slug>{_SLUG})#(?P<num>\d+)", value)
    if m:
        return int(m.group("num")), m.group("slug").lower()
    if value.lstrip("#").isdigit():
        return int(value.lstrip("#")), ""
    raise ValueError(f"pr must be a number, owner/name#N, or a PR URL — not {value!r}")
