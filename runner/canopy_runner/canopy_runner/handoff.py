"""A session's WORK, packed on one box and unpacked on another — the code half
of a transfer (`apps/canopy_sessions/handoff.py` is the server half).

A transfer used to carry the conversation's history and nothing else: the new
box started in a fresh worktree and was told "anything never pushed did not
come with you". Replaying the transcript is no substitute — measured on one
session, 6 of 11 uncommitted files had been changed by shell commands the
transcript only names. A diff is exact. So the SOURCE runner packs its own
worktree with plain git (no Claude tokens: an account out of tokens still has a
working runner), canopy-web holds it, and the TARGET runner unpacks it before the
agent looks:

* the branch and HEAD sha;
* local-only commits (`git bundle`, HEAD not on any remote-tracking ref);
* the staged and the unstaged diff, separately, so the index comes back too;
* untracked, non-ignored files — never an ignored one, and never one whose name
  says it is a secret (`.env`, keys), which is skipped and NAMED;
* optionally the Claude transcript, so a box that runs Claude itself can resume
  the conversation rather than start cold.

Restore refuses rather than clobbers: a worktree that already has changes, or one
of a different repo, is left alone and the report says why. It then checks its
own work — `git status`, HEAD and the diff must equal what the source had.

Stdlib only, and imported by BOTH runners (the cloud box puts this package on its
path, `_expose_repo_packages`), so the two cannot disagree about the format.
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

FORMAT = 1

#: The whole archive, compressed. canopy-web refuses more than this too.
MAX_ARCHIVE_BYTES = 24 * 1024 * 1024
#: One untracked file. A bigger one is skipped and named, not silently dropped.
MAX_FILE_BYTES = 8 * 1024 * 1024
#: Upload chunk: the JSON request (base64, +33%) stays under Django's 2.5 MB
#: request-body ceiling.
CHUNK_BYTES = 1536 * 1024

#: Untracked files never carried even when not ignored: their name says secret.
SECRET_PATTERNS = (
    ".env", ".env.*", "*.env", ".envrc", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", ".npmrc", ".pypirc", ".netrc",
    "*credentials*", "*secret*", ".git-credentials", "*.keystore",
)

#: Where the restore report lands: inside the worktree's own git dir, so it is
#: never part of `git status`, and the agent's first command can read it.
MARKER = "canopy-handoff.json"

#: How long the agent's first command waits for the marker before giving up.
WAIT_SECONDS = 180


class SnapshotError(RuntimeError):
    """The work could not be packed (too big, not a git worktree, git failed)."""


def _git(cwd: Path, *args: str, env: dict | None = None, timeout: float = 120,
         check: bool = True, text: bool = True, input=None):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=text,
                          timeout=timeout, env=env, input=input)
    if check and proc.returncode != 0:
        err = proc.stderr if text else proc.stderr.decode("utf-8", "replace")
        raise SnapshotError(f"git {args[0]} failed: {' '.join(str(err).split())[:300]}")
    return proc


def _out(cwd: Path, *args: str, **kw) -> str:
    return _git(cwd, *args, **kw).stdout.strip()


def is_secret_name(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return any(fnmatch.fnmatch(name, p) for p in SECRET_PATTERNS)


def status_entries(worktree: Path) -> list[str]:
    """`git status` as a sorted list of `XY path` entries, every untracked file
    listed on its own — the thing a restore must reproduce exactly."""
    raw = _git(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    entries, parts, i = [], raw.split("\0"), 0
    while i < len(parts):
        item = parts[i]
        i += 1
        if not item:
            continue
        if item[:1] in ("R", "C"):  # a rename carries its source as the next field
            item = f"{item} <- {parts[i] if i < len(parts) else ''}"
            i += 1
        entries.append(item)
    return sorted(entries)


def _diff_sha(worktree: Path) -> str:
    raw = _git(worktree, "diff", "--binary", "HEAD", text=False).stdout
    return hashlib.sha256(raw).hexdigest()


def normalize_remote(url: str) -> str:
    """github.com/org/repo for any spelling of it (https, ssh, with or without
    .git, with a token in it) — two checkouts of one repo compare equal."""
    u = (url or "").strip()
    if not u:
        return ""
    if "://" in u:
        u = u.split("://", 1)[1]
        u = u.split("@", 1)[-1]
    elif "@" in u and ":" in u:  # git@github.com:org/repo
        u = u.split("@", 1)[1].replace(":", "/", 1)
    u = u.rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    return u.lower()


def capture(worktree: Path, *, transcript: Path | None = None,
            max_bytes: int = MAX_ARCHIVE_BYTES) -> tuple[bytes, dict]:
    """Pack `worktree`'s work. Returns (archive bytes, manifest).

    The manifest is what canopy-web shows and tells the target agent: what was
    carried, and what was deliberately left behind (`skipped`). Raises
    SnapshotError when there is no git worktree or the work is over the cap even
    without the transcript."""
    worktree = Path(worktree)
    if not worktree.is_dir():
        raise SnapshotError(f"no worktree at {worktree}")
    try:
        top = _out(worktree, "rev-parse", "--show-toplevel")
    except (SnapshotError, OSError) as exc:
        raise SnapshotError(f"{worktree} is not a git worktree ({exc})") from exc
    worktree = Path(top)
    head = _out(worktree, "rev-parse", "HEAD")
    branch = _out(worktree, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    remote = _out(worktree, "remote", "get-url", "origin", check=False)
    upstream = _out(worktree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
                    check=False) if branch else ""
    local = int(_out(worktree, "rev-list", "--count", "HEAD", "--not", "--remotes") or 0)

    staged = _git(worktree, "diff", "--cached", "--binary", text=False).stdout
    unstaged = _git(worktree, "diff", "--binary", text=False).stdout
    staged_files = [p for p in _out(worktree, "diff", "--cached", "--name-only", "-z").split("\0") if p]
    unstaged_files = [p for p in _out(worktree, "diff", "--name-only", "-z").split("\0") if p]

    untracked, skipped = [], []
    for rel in _out(worktree, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if not rel:
            continue
        path = worktree / rel
        if is_secret_name(rel):
            skipped.append({"path": rel, "why": "its name says it holds a secret"})
        elif path.is_symlink() or not path.is_file():
            skipped.append({"path": rel, "why": "not a regular file"})
        elif path.stat().st_size > MAX_FILE_BYTES:
            skipped.append({"path": rel, "why": f"over {MAX_FILE_BYTES // (1024 * 1024)} MB"})
        else:
            untracked.append(rel)

    manifest = {
        "format": FORMAT,
        # Normalized, never the raw URL: a cloud clone's remote can carry a token.
        "repo": normalize_remote(remote),
        "branch": branch,
        "head": head,
        "upstream": upstream,
        "local_commits": local,
        "staged_files": staged_files,
        "unstaged_files": unstaged_files,
        "untracked_files": untracked,
        "skipped": skipped,
        "status": [e for e in status_entries(worktree)
                   if not (e.startswith("?? ") and e[3:] in {s["path"] for s in skipped})],
        "diff_sha256": _diff_sha(worktree),
        "transcript": None,
        "cwd": str(worktree),
    }

    with tempfile.TemporaryDirectory(prefix="canopy-handoff-") as tmp:
        bundle = Path(tmp) / "local.bundle"
        if local:
            _git(worktree, "bundle", "create", str(bundle), "HEAD", "--not", "--remotes")

        def pack(with_transcript: bool) -> bytes:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                def add(name: str, data: bytes) -> None:
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(data), 0o644
                    tar.addfile(info, io.BytesIO(data))

                m = dict(manifest)
                if with_transcript and transcript is not None:
                    data = Path(transcript).read_bytes()
                    m["transcript"] = {"session_id": Path(transcript).stem,
                                       "records": sum(1 for line in data.splitlines() if line.strip())}
                    add("transcript.jsonl", data)
                add("manifest.json", json.dumps(m, indent=1).encode())
                if bundle.exists():
                    add("local.bundle", bundle.read_bytes())
                add("staged.patch", staged)
                add("unstaged.patch", unstaged)
                for rel in untracked:
                    info = tarfile.TarInfo(f"untracked/{rel}")
                    st = (worktree / rel).stat()
                    info.size, info.mode = st.st_size, st.st_mode & 0o777
                    with open(worktree / rel, "rb") as fh:
                        tar.addfile(info, fh)
            return buf.getvalue()

        has_transcript = transcript is not None and Path(transcript).is_file()
        archive = pack(has_transcript)
        if len(archive) > max_bytes and has_transcript:
            manifest["transcript_dropped"] = "the transcript did not fit under the size cap"
            archive, has_transcript = pack(False), False
        if len(archive) > max_bytes:
            raise SnapshotError(f"the work is {len(archive) / 1048576:.1f} MB packed, "
                                f"over the {max_bytes / 1048576:.1f} MB cap")
    final = read_manifest(archive)
    final["bytes"] = len(archive)
    final["sha256"] = hashlib.sha256(archive).hexdigest()
    return archive, final


def _members(archive: bytes) -> dict[str, bytes]:
    out = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            fh = tar.extractfile(m)
            out[m.name] = fh.read() if fh is not None else b""
    return out


def read_manifest(archive: bytes) -> dict:
    members = _members(archive)
    if "manifest.json" not in members:
        raise SnapshotError("not a handoff archive (no manifest)")
    return json.loads(members["manifest.json"])


def read_transcript(archive: bytes) -> bytes | None:
    return _members(archive).get("transcript.jsonl")


def _safe_rel(rel: str) -> bool:
    p = PurePosixPath(rel)
    return bool(rel) and not p.is_absolute() and ".." not in p.parts and p.parts[0] != ".git"


def restore(worktree: Path, archive: bytes, *, env: dict | None = None) -> dict:
    """Unpack `archive` into the git worktree at `worktree` and check the result.

    Never raises for a refusal or a failed step: returns a report a person (and
    the agent) can read. `ok` is True only when `git status`, HEAD and the diff
    all equal the source's. `env` is the git environment for `fetch` (a cloud
    turn's GitHub identity)."""
    worktree = Path(worktree)
    report: dict = {"ok": False, "restored": False, "notes": [], "mismatch": []}
    try:
        members = _members(archive)
        manifest = json.loads(members["manifest.json"])
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"the snapshot could not be read: {exc}"
        return report
    report.update(branch=manifest.get("branch") or "", head=manifest.get("head") or "")
    try:
        _restore(worktree, manifest, members, report, env)
    except (SnapshotError, OSError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)[:500]
    return report


def _restore(worktree: Path, manifest: dict, members: dict, report: dict, env) -> None:
    if not (worktree / ".git").exists():
        raise SnapshotError(f"{worktree} is not a git worktree")
    here = normalize_remote(_out(worktree, "remote", "get-url", "origin", check=False))
    if manifest.get("repo") and here and here != manifest["repo"]:
        raise SnapshotError(f"this worktree is {here}, the snapshot is of {manifest['repo']}")
    before = status_entries(worktree)
    # Untracked files already here (a runtime's own settings, written as it made
    # the worktree) are not anyone's work: left alone and left out of the check —
    # unless the snapshot would write over one. Any change to a TRACKED file is
    # someone's work, and nothing is restored over it.
    preexisting = {e[3:] for e in before if e.startswith("?? ")}
    if any(not e.startswith("?? ") for e in before):
        raise SnapshotError("this worktree already has uncommitted changes, so nothing was "
                            "restored over them")
    clash = sorted(preexisting & {n[len("untracked/"):] for n in members
                                  if n.startswith("untracked/")})
    if clash:
        raise SnapshotError(f"untracked files here would be overwritten: {', '.join(clash[:5])}")
    if preexisting:
        report["notes"].append(f"left {len(preexisting)} untracked file(s) that were already "
                               "here alone")
    head, branch = manifest["head"], manifest.get("branch") or ""
    if _git(worktree, "fetch", "--quiet", "origin", env=env, check=False).returncode != 0:
        report["notes"].append("could not fetch origin; used what this clone already had")
    bundle = members.get("local.bundle")
    if bundle:
        with tempfile.NamedTemporaryFile(suffix=".bundle", delete=False) as fh:
            fh.write(bundle)
        try:
            _git(worktree, "fetch", "--quiet", fh.name, "HEAD")
        finally:
            os.unlink(fh.name)
    if _git(worktree, "cat-file", "-e", f"{head}^{{commit}}", check=False).returncode != 0:
        raise SnapshotError(f"commit {head[:12]} is neither on origin nor in the snapshot")

    if branch:
        if _git(worktree, "checkout", "-q", "-B", branch, head, check=False).returncode != 0:
            _git(worktree, "checkout", "-q", "--detach", head)
            report["notes"].append(f"branch {branch} is checked out elsewhere on this box; "
                                   f"restored detached at {head[:12]}")
            report["branch"] = ""
        elif _git(worktree, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}",
                  check=False).returncode == 0:
            _git(worktree, "branch", "-q", f"--set-upstream-to=origin/{branch}", branch, check=False)
    else:
        _git(worktree, "checkout", "-q", "--detach", head)

    for name, flags in (("staged.patch", ("--index",)), ("unstaged.patch", ())):
        patch = members.get(name) or b""
        if patch.strip():
            proc = _git(worktree, "apply", "--binary", *flags, "-", text=False, check=False,
                        input=patch)
            if proc.returncode != 0:
                raise SnapshotError(f"{name} did not apply: "
                                    f"{' '.join(proc.stderr.decode('utf-8', 'replace').split())[:300]}")
    written = 0
    for name, data in members.items():
        if not name.startswith("untracked/"):
            continue
        rel = name[len("untracked/"):]
        if not _safe_rel(rel):
            report["notes"].append(f"skipped an unsafe path in the snapshot: {rel!r}")
            continue
        dest = worktree / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        written += 1
    report["restored"] = True
    report["counts"] = {"local_commits": manifest.get("local_commits", 0),
                        "staged": len(manifest.get("staged_files") or []),
                        "unstaged": len(manifest.get("unstaged_files") or []),
                        "untracked": written}

    got = [e for e in status_entries(worktree) if not (e.startswith("?? ") and e[3:] in preexisting)]
    want = sorted(manifest.get("status") or [])
    if got != want:
        report["mismatch"].append({"what": "git status", "missing": sorted(set(want) - set(got)),
                                   "extra": sorted(set(got) - set(want))})
    if _out(worktree, "rev-parse", "HEAD") != head:
        report["mismatch"].append({"what": "HEAD"})
    if _diff_sha(worktree) != manifest.get("diff_sha256"):
        report["mismatch"].append({"what": "diff"})
    report["ok"] = not report["mismatch"]


def marker_path(worktree: Path) -> Path | None:
    try:
        gitdir = _out(Path(worktree), "rev-parse", "--absolute-git-dir")
    except (SnapshotError, OSError):
        return None
    return Path(gitdir) / MARKER


def write_marker(worktree: Path, report: dict) -> Path | None:
    """Leave the restore report where the agent's first command reads it."""
    path = marker_path(worktree)
    if path is None:
        return None
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=1))
    os.replace(tmp, path)
    return path


def summary(report: dict) -> str:
    """One line for a log or a result note."""
    if report.get("error"):
        return f"restore failed: {report['error']}"
    c = report.get("counts") or {}
    what = (f"{c.get('local_commits', 0)} local commit(s), {c.get('staged', 0)} staged, "
            f"{c.get('unstaged', 0)} unstaged, {c.get('untracked', 0)} untracked file(s)")
    where = report.get("branch") or "detached HEAD"
    if report.get("ok"):
        return f"restored {where} at {str(report.get('head'))[:12]} with {what}; git status matches"
    return f"restored {where} with {what}, but it does not match the source: {report.get('mismatch')}"


def chunks(archive: bytes, size: int = CHUNK_BYTES) -> list[bytes]:
    return [archive[i:i + size] for i in range(0, len(archive), size)] or [b""]
