"""A stored agent's git repository: objects and refs in the database, written with dulwich.

Each stored agent's definition is a real git repository (SHA-1, byte-compatible with
GitHub) that canopy-web hosts. Its objects are `AgentGitObject` rows and its refs
`AgentGitRef` rows, scoped to the agent: a Fargate task has no disk that lasts, so no
git binary and no working tree ever holds the state. This module is the git layer only:

* `AgentObjectStore` — dulwich's object store over the agent's rows: an object is read
  on demand (or every object at once, `preload`, to serve a fetch), and the objects a
  write adds are kept until `flush` inserts them;
* `write_tree` / `make_commit` / `files_at` / `read_path` — a file tree in and out of git;
* `refs_of` / `set_ref` / `delete_ref` — the refs, by name;
* `serving_repo` — a read-only repository to hand dulwich's upload-pack;
* `read_bundle` — a `git bundle` from a convert, every blob scanned for secrets first;
* `push` — sending refs to another git host (an export to GitHub).

The refs: `refs/heads/main` is the live published definition, `refs/heads/draft` the
one draft, `refs/tags/r<N>` published revision N, `refs/heads/imported` the history a
convert brought from GitHub. Who may change which ref, and the policy over every tree
canopy writes, is `apps/agent_definitions/services.py`; the smart HTTP endpoint that
serves the repository is `apps/agent_definitions/git_http.py`.
Doc: `docs/architecture/stored-agents.md`.
"""
from __future__ import annotations

import io
import time
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from canopy_sdk import agent_definition as policy
from dulwich.bundle import read_bundle as _read_bundle
from dulwich.object_store import MemoryObjectStore
from dulwich.objects import Blob, Commit, ShaFile, Tree
from dulwich.refs import DictRefsContainer
from dulwich.repo import BaseRepo

from .models import AgentGitObject, AgentGitRef

MAIN = "refs/heads/main"
DRAFT = "refs/heads/draft"
IMPORTED = "refs/heads/imported"
HEAD = "HEAD"
TAG_PREFIX = "refs/tags/r"

#: Who commits every revision: canopy, whoever wrote it.
COMMITTER = b"canopy <agents@canopy.dimagi.com>"
#: The author of a revision an agent wrote itself (its own login, or a turn's token).
AGENT_AUTHOR_EMAIL = "agent@canopy.dimagi.com"

_FILE_MODE = 0o100644
_EXEC_MODE = 0o100755
_DIR_MODE = 0o040000


def tag_ref(number: int) -> str:
    return f"{TAG_PREFIX}{number}"


class GitError(Exception):
    """A repository canopy will not take or cannot write. `code` is stable for a client;
    `problems` is the policy's list (`Problem.as_dict()`), when there is one."""

    def __init__(self, message: str, *, code: str, status: int = 422, problems: list | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.problems = problems or []


# --- the object store -------------------------------------------------------


class AgentObjectStore(MemoryObjectStore):
    """dulwich's object store over one agent's `AgentGitObject` rows.

    Reads go to the rows on demand and are cached; `preload` reads every row in one
    query (a fetch walks the whole graph, so it should not ask object by object). Added
    objects are held in memory until `flush` inserts them; nothing is ever updated or
    deleted. `model` is the row class — a data migration passes its historical one.
    `owner` names the row's owner column: `agent_id`, or `project_id` for a project's
    repository (`ProjectGitObject`, `apps/agents/project_files.py`)."""

    def __init__(self, agent_id: int, *, model=AgentGitObject, owner: str = "agent_id") -> None:
        super().__init__()
        self.agent_id = agent_id
        self._model = model
        self._owner = owner
        self._new: dict[bytes, ShaFile] = {}
        self._complete = False

    # reading

    def _row(self, sha: bytes) -> ShaFile | None:
        hexsha = sha.decode("ascii")
        row = self._model.objects.filter(**{self._owner: self.agent_id}, sha=hexsha).values_list("type", "data").first()
        if row is None:
            return None
        obj = ShaFile.from_raw_string(row[0], zlib.decompress(bytes(row[1])), sha=sha)
        self._data[sha] = obj
        return obj

    def preload(self) -> AgentObjectStore:
        if not self._complete:
            for sha, type_num, data in (self._model.objects.filter(**{self._owner: self.agent_id})
                                        .values_list("sha", "type", "data").iterator()):
                key = sha.encode("ascii")
                if key not in self._data:
                    self._data[key] = ShaFile.from_raw_string(type_num, zlib.decompress(bytes(data)), sha=key)
            self._complete = True
        return self

    def contains_loose(self, sha) -> bool:
        key = self._to_hexsha(sha)
        return key in self._data or (not self._complete and self._row(key) is not None)

    def __getitem__(self, name) -> ShaFile:
        key = self._to_hexsha(name)
        obj = self._data.get(key)
        if obj is None and not self._complete:
            obj = self._row(key)
        if obj is None:
            raise KeyError(name)
        return obj.copy()

    def get_raw(self, name) -> tuple[int, bytes]:
        obj = self[name]
        return obj.type_num, obj.as_raw_string()

    def __iter__(self) -> Iterator[bytes]:
        self.preload()
        return iter(list(self._data.keys()))

    # writing

    def add_object(self, obj: ShaFile) -> None:
        if obj.id in self._data or obj.id in self._new:
            return
        if not self._complete and self._row(obj.id) is not None:
            return
        self._data[obj.id] = obj.copy()
        self._new[obj.id] = obj

    def flush(self) -> int:
        """Insert the objects added since the last flush; return how many."""
        rows = [self._model(**{self._owner: self.agent_id}, sha=sha.decode("ascii"), type=obj.type_num,
                            data=zlib.compress(obj.as_raw_string()))
                for sha, obj in self._new.items()]
        if rows:
            self._model.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)
        self._new.clear()
        return len(rows)


# --- refs -------------------------------------------------------------------


def refs_of(agent_id: int, *, model=AgentGitRef, owner: str = "agent_id") -> dict[str, str]:
    """{ref name: sha} of the agent's repository (or, with `owner="project_id"`, a project's)."""
    return dict(model.objects.filter(**{owner: agent_id}).values_list("name", "sha"))


def ref(agent_id: int, name: str, *, model=AgentGitRef, owner: str = "agent_id") -> str:
    return model.objects.filter(**{owner: agent_id}, name=name).values_list("sha", flat=True).first() or ""


def set_ref(agent_id: int, name: str, sha: str, *, model=AgentGitRef, owner: str = "agent_id") -> None:
    model.objects.update_or_create(**{owner: agent_id}, name=name, defaults={"sha": sha})


def delete_ref(agent_id: int, name: str, *, model=AgentGitRef, owner: str = "agent_id") -> None:
    model.objects.filter(**{owner: agent_id}, name=name).delete()


# --- trees and commits -------------------------------------------------------


def write_tree(store: AgentObjectStore, files: dict[str, tuple[bytes, bool]]) -> bytes:
    """Add `files` (`{path: (content, executable)}`) to `store` as blobs and trees; return
    the root tree's sha."""
    root: dict = {}
    for path, (data, executable) in files.items():
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        blob = Blob.from_string(bytes(data))
        store.add_object(blob)
        node[parts[-1]] = (_EXEC_MODE if executable else _FILE_MODE, blob.id)

    def build(node: dict) -> bytes:
        tree = Tree()
        for name, value in node.items():
            if isinstance(value, dict):
                tree.add(name.encode("utf-8"), _DIR_MODE, build(value))
            else:
                tree.add(name.encode("utf-8"), value[0], value[1])
        store.add_object(tree)
        return tree.id

    return build(root)


def make_commit(store: AgentObjectStore, *, tree: bytes, parents: Iterable[str], author: bytes,
                message: str, when: int | None = None) -> str:
    """Add a commit of `tree` to `store`; return its sha (hex)."""
    now = int(when if when is not None else time.time())
    commit = Commit()
    commit.tree = tree
    commit.parents = [p.encode("ascii") for p in parents if p]
    commit.author = author
    commit.committer = COMMITTER
    commit.author_time = commit.commit_time = now
    commit.author_timezone = commit.commit_timezone = 0
    commit.encoding = b"UTF-8"
    text = (message or "").strip() or "(no message)"
    commit.message = (text + "\n").encode("utf-8")
    store.add_object(commit)
    return commit.id.decode("ascii")


def person_author(user) -> bytes:
    name = (user.get_full_name() or "").strip() or user.email
    return identity(name, user.email)


def agent_author(slug: str) -> bytes:
    return identity(slug, AGENT_AUTHOR_EMAIL)


def identity(name: str, email: str) -> bytes:
    clean = " ".join(str(name).replace("<", "").replace(">", "").split()) or "canopy"
    return f"{clean} <{str(email).replace('<', '').replace('>', '').strip()}>".encode()


def _walk(store, tree_sha: bytes, prefix: str = "") -> Iterator[tuple[str, int, bytes]]:
    tree = store[tree_sha]
    for entry in tree.iteritems():
        name = entry.path.decode("utf-8", errors="surrogateescape")
        path = f"{prefix}{name}"
        if entry.mode & 0o170000 == _DIR_MODE:
            yield from _walk(store, entry.sha, path + "/")
        else:
            yield path, entry.mode, entry.sha


def files_at(store, commit_sha: str) -> dict[str, tuple[bytes, bool]]:
    """{path: (content, executable)} of a commit's tree, sorted by path."""
    commit = store[commit_sha.encode("ascii")]
    out = {path: (store[sha].as_raw_string(), mode == _EXEC_MODE)
           for path, mode, sha in _walk(store, commit.tree)}
    return dict(sorted(out.items()))


def read_path(store, commit_sha: str, path: str) -> bytes | None:
    commit = store[commit_sha.encode("ascii")]
    try:
        mode, sha = commit_tree_lookup(store, commit.tree, path)
    except KeyError:
        return None
    if mode & 0o170000 == _DIR_MODE:
        return None
    return store[sha].as_raw_string()


def commit_tree_lookup(store, tree_sha: bytes, path: str) -> tuple[int, bytes]:
    node = tree_sha
    mode = _DIR_MODE
    for part in path.split("/"):
        if mode & 0o170000 != _DIR_MODE:
            raise KeyError(path)
        tree = store[node]
        try:
            mode, node = tree[part.encode("utf-8")]
        except KeyError:
            raise KeyError(path) from None
    return mode, node


def manifest(files: dict[str, tuple[bytes, bool]]) -> dict[str, dict]:
    """The revision's `tree` field: `{path: {sha256, size, executable}}`."""
    return {path: {"sha256": policy.sha256_hex(data), "size": len(data), "executable": bool(executable)}
            for path, (data, executable) in sorted(files.items())}


# --- serving ------------------------------------------------------------------


def serving_repo(store: AgentObjectStore, refs: dict[str, str]) -> BaseRepo:
    """A read-only repository of `refs` (already filtered to what the reader may see),
    with HEAD naming `refs/heads/main` when it is among them, for dulwich's upload-pack."""
    container = DictRefsContainer({name.encode("utf-8"): sha.encode("ascii") for name, sha in refs.items()})
    if MAIN in refs:
        container.set_symbolic_ref(HEAD.encode(), MAIN.encode())
    return BaseRepo(store, container)


# --- importing a bundle ------------------------------------------------------


@dataclass
class ImportedHistory:
    """What a convert's bundle brought: its tip commit and every object in it."""

    tip: str
    objects: list[ShaFile]


def read_bundle(data: bytes) -> ImportedHistory:
    """The history in a `git bundle` of one branch (the CLI sends `git bundle create - main`).

    Refused (GitError): anything that is not a complete bundle of SHA-1 objects, a bundle
    with prerequisites (a partial history), one naming no branch, and — before anything is
    kept — one where any blob, in any commit, looks like it holds a secret (`secret`,
    with each path and commit where it was found)."""
    try:
        bundle = _read_bundle(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — dulwich raises several kinds for a bad bundle
        raise GitError(f"bundle_b64 is not a git bundle ({str(exc)[:200]})", code="bad_bundle") from None
    try:
        if bundle.prerequisites:
            raise GitError("the bundle is a partial history (it has prerequisites); send the whole "
                           "branch with `git bundle create - main`", code="bad_bundle")
        heads = {name.decode("utf-8", "replace"): sha.decode("ascii") for name, sha in bundle.references.items()}
        tip = heads.get(MAIN) or next((sha for name, sha in sorted(heads.items())
                                       if name.startswith("refs/heads/")), "") or heads.get(HEAD, "")
        if not tip:
            raise GitError("the bundle names no branch", code="bad_bundle")
        scratch = MemoryObjectStore()
        if bundle.pack_data is not None:
            bundle.store_objects(scratch)
        if tip.encode("ascii") not in scratch:
            raise GitError("the bundle does not hold the commit its branch names", code="bad_bundle")
    finally:
        bundle.close()
    objects = [scratch[sha] for sha in scratch]
    problems = _secret_problems(scratch, objects)
    if problems:
        raise GitError(f"the imported history holds something that looks like a secret — "
                       f"{problems[0]['path']}: {problems[0]['message']}"
                       + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""),
                       code="secret", problems=problems)
    return ImportedHistory(tip=tip, objects=objects)


def _secret_problems(store, objects: list[ShaFile]) -> list[dict]:
    """Every blob in `objects` with a secret-shaped string, named by a path and commit it
    appears at (a blob no commit reaches is named by its sha)."""
    where: dict[bytes, str] = {}
    for obj in objects:
        if isinstance(obj, Commit):
            short = obj.id.decode("ascii")[:12]
            try:
                for path, _mode, sha in _walk(store, obj.tree):
                    where.setdefault(sha, f"{path} @ {short}")
            except KeyError:
                continue
    problems = []
    for obj in objects:
        if not isinstance(obj, Blob):
            continue
        kinds = policy.secret_kinds(obj.as_raw_string().decode("utf-8", errors="replace"))
        if kinds:
            problems.append({"path": where.get(obj.id, f"blob {obj.id.decode('ascii')[:12]}"), "code": "secret",
                             "message": f"looks like it holds a secret ({', '.join(kinds)}); a secret in "
                                        "history is still a secret — remove it from the history, or "
                                        "convert without it"})
    return sorted(problems, key=lambda p: p["path"])


# --- pushing ---------------------------------------------------------------


def remote_refs(url: str, **auth) -> dict[str, str]:
    """{ref: sha} of the repository at `url` (empty for an empty repository). Raises
    whatever dulwich raises for one that cannot be reached."""
    from dulwich.client import get_transport_and_path  # noqa: PLC0415 — only an export pays for it

    client, path = get_transport_and_path(url, **auth)
    result = client.get_refs(path.encode("utf-8") if isinstance(path, str) else path)
    refs = getattr(result, "refs", result) or {}
    return {k.decode("utf-8", "replace"): v.decode("ascii") for k, v in refs.items()
            if v is not None and k != b"HEAD" and not k.endswith(b"^{}")}


def push(store: AgentObjectStore, url: str, updates: dict[str, str], **auth) -> None:
    """Send `updates` (`{ref: sha}`) and every object they reach to the repository at
    `url`, which must not have them yet. Raises GitError naming each ref it refused."""
    from dulwich.client import get_transport_and_path  # noqa: PLC0415 — only an export pays for it

    store.preload()
    client, path = get_transport_and_path(url, **auth)
    wanted = {k.encode("utf-8"): v.encode("ascii") for k, v in updates.items()}
    result = client.send_pack(path.encode("utf-8") if isinstance(path, str) else path,
                              lambda _old: dict(wanted), store.generate_pack_data)
    failed = {k.decode("utf-8", "replace"): v for k, v in (getattr(result, "ref_status", None) or {}).items() if v}
    if failed:
        raise GitError("the push was refused: " + "; ".join(f"{k}: {v}" for k, v in sorted(failed.items())),
                       code="push_refused", status=502)
