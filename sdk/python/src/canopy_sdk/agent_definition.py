"""A stored agent's definition: the git repository canopy hosts for an agent with no GitHub repo.

A repo agent's definition is its GitHub repository. A STORED agent's is a git repository
canopy-web hosts and is the only writer of: each revision is a commit, a draft is
published by a person who administers the agent, and people and runners clone and fetch
it read-only from ``git_url(base, workspace, slug)`` with a canopy token.

This module is the part both sides must compute identically, so it lives here once:

* ``manifest_root(files)`` — a revision's manifest root: the sha256 of its canonical
  manifest. canopy-web reports it with every revision; a client checks a tree against it.
* ``check_tree(files, slug=...)`` — the file policy. canopy-web enforces it on every
  commit it writes; the CLI asks it first so a person hears the refusal before uploading.
* ``git_url`` / ``is_git_url`` — where a stored agent's repository is, and whether a
  clone's origin is one (the canopy CLI's fleet updater fetches those with a token header).

Standard library only. Contract: canopy-web ``docs/architecture/stored-agents.md``.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: The most files one revision may hold.
MAX_FILES = 200
#: The largest one file may be, in bytes.
MAX_FILE_BYTES = 256 * 1024
#: The largest a whole revision may be, in bytes.
MAX_TREE_BYTES = 2 * 1024 * 1024
#: The longest a path may be, in characters.
MAX_PATH_CHARS = 200

#: The paths a person or an agent may write, as globs (``<name>`` is one path segment;
#: ``**`` is any number of segments). Shown to people as the policy; enforced by
#: ``editable`` below, not by matching these strings.
EDITABLE_GLOBS: tuple[str, ...] = (
    "CLAUDE.md",
    "persona.md",
    "README.md",
    "skills/<name>/SKILL.md",
    "skills/<name>/**/*.{md,json,yaml,yml,txt}",
    "config/agent.json",
    "config/gating.json",
    "config/allowlist.txt",
    "tests/**/*.{json,yaml,yml,md,txt}",
    "docs/**/*.md",
)

#: The paths canopy writes itself, on every publish. Refused as edits.
GENERATED_GLOBS: tuple[str, ...] = (
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    ".claude/settings.json",
    "bin/<slug>-email",
    ".gitignore",
)

_TOP_FILES = frozenset({"CLAUDE.md", "persona.md", "README.md"})
_CONFIG_FILES = frozenset({"config/agent.json", "config/gating.json", "config/allowlist.txt"})
_DATA_EXTS = frozenset({"md", "json", "yaml", "yml", "txt"})
#: One path segment: letters, digits, `.`, `_`, `-`; never starting with `.` (no hidden
#: files, no `.git`, no `..`).
_SEGMENT = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def generated_paths(slug: str) -> frozenset[str]:
    """The exact server-generated paths for agent ``slug``."""
    return frozenset({
        ".claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        ".claude/settings.json",
        f"bin/{slug}-email",
        ".gitignore",
    })


def executable_paths(slug: str) -> frozenset[str]:
    """The only paths that may be executable: the email shim canopy writes."""
    return frozenset({f"bin/{slug}-email"})


def _ext(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def path_problem(path: str) -> str | None:
    """Why ``path`` is not a well-formed repo-relative path, or None."""
    if not isinstance(path, str) or not path:
        return "empty path"
    if len(path) > MAX_PATH_CHARS:
        return f"longer than {MAX_PATH_CHARS} characters"
    if "\x00" in path:
        return "contains NUL"
    if "\\" in path:
        return "contains a backslash"
    if path.startswith("/"):
        return "absolute path"
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "empty, '.' or '..' segment"
    if parts[0] == ".git" or ".git" in parts:
        return "inside .git/"
    return None


def editable(path: str) -> bool:
    """May a person or an agent write ``path``? (Server-generated paths are not.)"""
    if path_problem(path) is not None:
        return False
    parts = path.split("/")
    if not all(_SEGMENT.match(p) for p in parts):
        return False
    if path in _TOP_FILES or path in _CONFIG_FILES:
        return True
    head = parts[0]
    if head == "skills":
        if len(parts) < 3 or not _SKILL_NAME.match(parts[1]):
            return False
        if len(parts) == 3 and parts[2] == "SKILL.md":
            return True
        return _ext(path) in _DATA_EXTS
    if head == "tests":
        return len(parts) >= 2 and _ext(path) in _DATA_EXTS
    if head == "docs":
        return len(parts) >= 2 and _ext(path) == "md"
    return False


# --- secret shapes --------------------------------------------------------
# The shaped secrets canopy-web's transcript scrubber knows (`apps/common/redact.py`),
# kept here as detectors: a definition is code every runner of the agent reads, so a
# value of this shape in it is refused, never stored. A test in canopy-web keeps the
# two lists agreeing.

SECRET_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("canopy-token", re.compile(r"\b(?:cpat|cdt|ccon|cct|crt|chk|hcpat|hcprt|cact)_[A-Za-z0-9_-]{20,}")),
    ("sealed-secret", re.compile(r"\bzks1[.:][A-Za-z0-9_.:+/=-]{16,}")),
    ("google-token", re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}")),
    ("url-credential", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@<>{}$]+:[^\s/@<>{}$]{3,}@")),
    ("basic-auth", re.compile(r"\b[Aa]uthorization\b\s*[:=]\s*[Bb]asic\s+[A-Za-z0-9+/=]{8,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer", re.compile(r"\b[Aa]uthorization\b\s*[:=]\s*[Bb]earer\s+[A-Za-z0-9._~+/=-]{20,}")),
)

#: `NAME = value` for a secret-sounding NAME, with a literal value. A placeholder
#: (`$VAR`, `<token>`, `{{x}}`, `...`, `your-…`, `xxx…`) is not a value.
_ASSIGNMENT = re.compile(
    r"\b([A-Za-z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|"
    r"ACCESS[_-]?KEY|CLIENT[_-]?SECRET)[A-Za-z0-9_]*)\s*[:=]\s*(['\"]?)([^\s'\"]{12,})\2",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(r"^(?:[$<{(\[*.]|your|my-|xxx|example|placeholder|redacted|changeme|‹)", re.IGNORECASE)


def secret_kinds(text: str) -> list[str]:
    """The kinds of secret-shaped strings in ``text`` (empty when none)."""
    found = [kind for kind, pattern in SECRET_SHAPES if pattern.search(text)]
    for m in _ASSIGNMENT.finditer(text):
        value = m.group(3)
        # A value with no digit is a word, not a credential (`TOKEN: required`).
        if not _PLACEHOLDER.match(value) and re.search(r"\d", value) and re.search(r"[A-Za-z]", value):
            found.append("secret-assignment")
            break
    return sorted(set(found))


# --- the policy over a whole tree -----------------------------------------


@dataclass(frozen=True)
class Problem:
    """One reason a tree is refused: ``path`` ("" for the whole tree), a stable ``code``
    and words for a person."""

    path: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


def _content_bytes(content: Any) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray | memoryview):
        return bytes(content)
    if isinstance(content, str):
        return content.encode("utf-8")
    raise TypeError("content must be bytes or str")


def _entries(files: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]]):
    if isinstance(files, Mapping):
        for path, f in files.items():
            yield {"path": path, **dict(f)}
    else:
        for f in files:
            yield dict(f)


def check_tree(files: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]], *, slug: str,
               allow_generated: bool = False) -> list[Problem]:
    """Every reason ``files`` may not be a stored agent's tree; empty when it may.

    ``files`` is a list of ``{"path", "content" (bytes or str), "executable"?, "symlink"?}``
    or a ``{path: {...}}`` mapping. ``allow_generated`` is canopy-web's check of a whole
    revision, which holds the files it generated; a person's or an agent's edit is checked
    without it, so a generated path in it is refused (``server_generated``).

    Codes: ``bad_path``, ``hooks``, ``not_editable``, ``server_generated``,
    ``executable``, ``symlink``, ``duplicate``, ``too_large``, ``too_many_files``,
    ``tree_too_large``, ``not_utf8``, ``secret``.
    """
    problems: list[Problem] = []
    generated = generated_paths(slug)
    executables = executable_paths(slug)
    seen: set[str] = set()
    total = 0
    count = 0
    for f in _entries(files):
        path = f.get("path")
        count += 1
        why = path_problem(path)
        if why is not None:
            problems.append(Problem(str(path or ""), "bad_path", f"not a valid path: {why}"))
            continue
        if path in seen:
            problems.append(Problem(path, "duplicate", "listed more than once"))
            continue
        seen.add(path)
        if f.get("symlink"):
            problems.append(Problem(path, "symlink", "a symbolic link; a stored agent holds files only"))
            continue
        if path.split("/", 1)[0] == "hooks":
            problems.append(Problem(path, "hooks", "a stored agent has no hooks"))
            continue
        if path in generated:
            if not allow_generated:
                problems.append(Problem(path, "server_generated",
                                        "canopy writes this file itself on every publish"))
                continue
        elif not editable(path):
            problems.append(Problem(path, "not_editable",
                                    "not a path a stored agent may hold (see the editable paths)"))
            continue
        if f.get("executable") and path not in executables:
            problems.append(Problem(path, "executable", "only canopy's own bin/<slug>-email is executable"))
        try:
            data = _content_bytes(f.get("content", b""))
        except TypeError:
            problems.append(Problem(path, "not_utf8", "content is not text"))
            continue
        total += len(data)
        if len(data) > MAX_FILE_BYTES:
            problems.append(Problem(path, "too_large", f"larger than {MAX_FILE_BYTES // 1024} KB"))
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(Problem(path, "not_utf8", "not UTF-8 text"))
            continue
        if "\x00" in text:
            problems.append(Problem(path, "not_utf8", "contains NUL"))
            continue
        kinds = secret_kinds(text)
        if kinds:
            problems.append(Problem(path, "secret", f"looks like it holds a secret ({', '.join(kinds)}); "
                                    "secrets go in the agent's vault, never its definition"))
    if count > MAX_FILES:
        problems.append(Problem("", "too_many_files", f"more than {MAX_FILES} files"))
    if total > MAX_TREE_BYTES:
        problems.append(Problem("", "tree_too_large", f"larger than {MAX_TREE_BYTES // (1024 * 1024)} MB in all"))
    return problems


# --- the manifest ---------------------------------------------------------


def sha256_hex(content: bytes | str) -> str:
    return hashlib.sha256(_content_bytes(content)).hexdigest()


def manifest_entry(path: str, content: bytes | str, executable: bool = False) -> dict[str, Any]:
    """One file's line in the manifest."""
    data = _content_bytes(content)
    return {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "executable": bool(executable)}


def canonical_manifest(files: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]]) -> bytes:
    """The canonical manifest: ``[{"path","sha256","size","executable"}]`` sorted by path,
    as compact JSON with sorted keys. Each entry carries those four fields only."""
    rows = [{"path": e["path"], "sha256": e["sha256"], "size": int(e["size"]),
             "executable": bool(e.get("executable", False))} for e in _entries(files)]
    rows.sort(key=lambda r: r["path"])
    return json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")


def manifest_root(files: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]]) -> str:
    """The sha256 (hex) of ``canonical_manifest(files)``: a revision's identity.

    ``files`` is the manifest entries (``path``, ``sha256``, ``size``, ``executable``), or
    canopy-web's stored tree ``{path: {"sha256", "size", "executable"}}``."""
    return hashlib.sha256(canonical_manifest(files)).hexdigest()


def manifest_root_of_files(files: Iterable[Mapping[str, Any]]) -> str:
    """``manifest_root`` of files given with their content (``path``, ``content``,
    ``executable``) — e.g. an extracted archive."""
    return manifest_root([manifest_entry(f["path"], f["content"], bool(f.get("executable")))
                          for f in files])


def git_url(base_url: str, workspace: str, slug: str) -> str:
    """The address of a stored agent's repository: ``<base>/git/<workspace>/<slug>.git``."""
    return f"{base_url.rstrip('/')}/git/{workspace}/{slug}.git"


def is_git_url(url: str, base_url: str) -> bool:
    """Is ``url`` a stored agent's repository on the canopy at ``base_url``?"""
    url = (url or "").strip().rstrip("/")
    base = (base_url or "").strip().rstrip("/")
    if not base or not url.startswith(f"{base}/git/") or not url.endswith(".git"):
        return False
    parts = url[len(base) + len("/git/"):].split("/")
    return len(parts) == 2 and all(parts) and parts[1] != ".git"


def policy() -> dict[str, Any]:
    """The policy as data, for a person reading it (canopy-web's ``GET …/definition``)."""
    return {
        "max_files": MAX_FILES,
        "max_file_bytes": MAX_FILE_BYTES,
        "max_tree_bytes": MAX_TREE_BYTES,
        "editable": list(EDITABLE_GLOBS),
        "generated": list(GENERATED_GLOBS),
    }


__all__ = [
    "EDITABLE_GLOBS", "GENERATED_GLOBS", "MAX_FILES", "MAX_FILE_BYTES", "MAX_TREE_BYTES", "Problem",
    "SECRET_SHAPES", "canonical_manifest", "check_tree", "editable", "executable_paths", "generated_paths",
    "git_url", "is_git_url", "manifest_entry", "manifest_root", "manifest_root_of_files", "path_problem", "policy", "secret_kinds",
    "sha256_hex",
]
