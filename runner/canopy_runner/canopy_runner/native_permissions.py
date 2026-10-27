"""Claude Code's OWN permission rules for a caller's (`cx-`) session — the second layer.

canopy's `profile_guard` PreToolUse hook is the exact layer: argv token for token,
`{thread_id}`-pinned, paths realpath'd, fail closed. It is also the ONLY layer, and
it is a hook — the same mechanism a plugin update, a malformed hooks.json or a
`disableAllHooks` can take out. This module writes the capability again in the one
language Claude Code enforces itself: permission rules, in the session worktree's
`.claude/settings.json`.

What actually holds, and why it is shaped this way (verified against
code.claude.com/docs/en/permissions + permission-modes + settings, 2026-09-26, and
against emdash's own launcher):

* **emdash starts fleet sessions in bypass.** It passes
  `--dangerously-skip-permissions` whenever its "Auto-approve permissions" switch is
  on, and that switch is ONE localStorage value shared with every task a human
  creates — flipping it for a cx- task would flip it for the next human task too.
  A CLI flag outranks `defaultMode`, so a settings file cannot take a session out of
  bypass.
* **Deny rules hold in every mode, bypass included** ("Deny rules block in every
  mode, including `bypassPermissions`"), need no workspace trust ("`deny` and `ask`
  rules aren't affected, since they only restrict"), and beat any allow rule or hook
  "allow". So the DENY list is the layer that takes effect today. It can only remove
  what the capability does not grant at all — an allow cannot carve an exception out
  of a deny — so it removes whole tools, whole MCP servers, high-risk programs the
  capability's Bash patterns never start with, and credential directories.
* **`defaultMode: dontAsk` + allow rules** are written too. Under bypass they do
  nothing; they are what makes the session a strict allowlist the day a cx- session
  starts WITHOUT bypass (a resumed conversation, an emdash that honours a per-task
  switch), where the alternative is a permission prompt no human is watching.
* **`.claude/settings.json`, not `settings.local.json`.** In a git worktree Claude
  Code reads `settings.local.json` from the MAIN checkout's root — one file for every
  session of that agent — while `.claude/settings.json` is read from the session's own
  working directory. It is also hot-reloaded, "even when you create the folder in the
  same session", which matters: a NEW session's worktree does not exist until the
  click that also submits its first prompt, so for the first seconds of a new cx-
  session `profile_guard` is the only layer.
* **File writes are `Edit(path)` rules, never `Write(path)`.** Claude Code matches a
  path against `Edit(…)` rules for EVERY file-changing tool (Write, Edit,
  NotebookEdit) and ignores a `Write(path)` rule outright — it prints "is not matched
  by file permission checks — only Edit(path) rules are" at startup and enforces
  nothing (#1058; measured on 2.1.287: a `Write(path)` deny let the Write tool write
  there, an `Edit(path)` deny stopped it). A BARE tool name is different: `Edit` in
  `deny` removes only the tool called Edit, so a capability granting Write but not
  Edit keeps Write, scoped by the `Edit(path)` allows. Writes are scoped by
  `write_paths`, as `profile_guard` scopes them: none → no file writes at all.
* **MCP rules name REAL servers.** An allow rule whose server segment holds a glob
  (`mcp__*canopy-web__who_is_asking`, the form interfaces are written in) is skipped
  by Claude Code, so patterns are normalised against the servers configured on this
  box (`plugin_canopy_canopy-web`, …). A claude.ai connector cannot be enumerated
  offline and is left to the hook unless the capability grants no MCP at all, in
  which case `mcp__*` (a glob deny is legal) removes every server.
* **A server the capability never reaches is not STARTED.** An `mcp__<server>` deny
  removes its tools but Claude Code still spawns and connects it (measured on
  2.1.287: every denied plugin server started, ~3s of stdio launches before the first
  request on a laptop, plus a connect timeout for any that hang). `deniedMcpServers`
  — named by CONFIG name (`plugin:ace:ace-ocs`), read from `.claude/settings.json`
  and from `--settings` alike — keeps it from starting at all. Documented as a
  managed-settings key; honouring it from project settings was measured, not read,
  so the `mcp__<server>` deny stays beside it: if a release ever ignores the key, the
  server starts again and its tools are still refused.

Pure and stdlib-only: the cloud runner imports it too.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import pathlib
import re
import shlex

logger = logging.getLogger(__name__)

#: Built-in tools a capability either grants or does not. `Read` is deliberately
#: absent: the session is TOLD to read its own envelope (`--caller <path>`) even when
#: its capability lists no Read, and a deny cannot make that one exception. `Skill`
#: is absent too: a caller's session STARTS with a skill's slash command (the
#: capability's `entry`, e.g. `/ace:ask`), and a native deny that removed the skill
#: machinery would be a session that cannot begin. It only loads instructions; every
#: action a skill then takes is a tool call, gated here and by the hook.
GATED_TOOLS = ("Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch",
               "Agent", "Glob", "Grep")

#: Programs denied outright unless one of the capability's own Bash patterns starts
#: with them. A deny rule "isn't a security boundary around the program" (another
#: spelling of the same call slips past it) — profile_guard's argv match is the
#: boundary; this removes the common spellings of the dangerous ones a second time.
HIGH_RISK_PROGRAMS = (
    "git", "gh", "curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "telnet",
    "rm", "mv", "dd", "chmod", "chown", "ln", "kill", "pkill",
    "aws", "gcloud", "az", "docker", "kubectl", "terraform", "op", "security",
    "npm", "npx", "yarn", "pnpm", "pip", "pip3", "uv", "python", "python3", "node",
    "deno", "bun", "bash", "sh", "zsh", "fish", "eval", "exec", "sudo", "su",
    "osascript", "open", "launchctl", "crontab",
)

#: Tools that CHANGE the file at their path — `profile_guard`'s `_WRITE_TOOLS`.
#: Claude Code scopes all of them with one rule form, `Edit(path)`.
WRITE_TOOLS = ("Write", "Edit", "NotebookEdit")

#: Credential stores a caller's session never needs, whatever its paths say — denied
#: to Read when every read_path is inside the worktree, and to every file-writing tool
#: (as `Edit(…)`) when every write_path is. Also every Claude transcript on the box
#: (the CLI writes and resumes its own without a tool call; a session's oversized tool
#: output, saved beside its transcript, stays readable) and the per-turn state other
#: sessions keep under ~/.canopy: the conversations a sensitive-data box keeps, chat
#: keys, scoped and agent tokens, every process's command line and environment
#: (`/proc`), and browser profiles. The other conversations' sessions and the other
#: turns' caller envelopes are denied by PATTERN beside these (`reach_denies`): every
#: name but the session's own, so a directory or envelope made after the rules were
#: written is covered too.
#:
#: Denies matter even where an allow list scopes the read tools: Claude Code expands
#: an `@path` in a prompt into the file's contents before the model runs, with no
#: tool call, and that read honours deny rules but not the allow list. A caller's
#: words are neutralised before they reach a prompt (`caller.neutralise_mentions`);
#: these are what holds if one ever is not.
SENSITIVE_PATHS = (
    "~/.ssh/**", "~/.aws/**", "~/.gnupg/**", "~/.config/gh/**", "~/.config/op/**",
    "~/.docker/**", "~/.netrc", "~/.claude.json", "~/.claude/canopy/**",
    "~/.canopy/profiles/**", "~/.canopy/caller/pending/**", "~/.canopy/runner.json",
    "~/.claude/projects/**/*.jsonl", "~/.claude/history.jsonl", "~/.claude/.credentials.json",
    "~/.canopy/kept-conversations/**", "~/.canopy/chat/**", "~/.canopy/scoped/**",
    "~/.canopy/agent-token/**", "~/.canopy/delegated/**", "~/.canopy/device-key/**",
    "/proc/**",
    # Browser profiles (cookies, saved logins, sessions): gstack's headless browser,
    # and Chrome, Chromium and the other common browsers, on macOS, Linux and Windows.
    "~/.gstack/**",
    "~/Library/Application Support/Google/**", "~/Library/Application Support/Chromium/**",
    "~/Library/Application Support/BraveSoftware/**",
    "~/Library/Application Support/Microsoft Edge/**", "~/Library/Application Support/Arc/**",
    "~/Library/Application Support/Firefox/**",
    "~/.config/google-chrome/**", "~/.config/google-chrome-beta/**",
    "~/.config/google-chrome-unstable/**", "~/.config/chromium/**",
    "~/.config/BraveSoftware/**", "~/.config/microsoft-edge/**", "~/.mozilla/**",
    "~/snap/chromium/**", "~/.var/app/com.google.Chrome/**",
    "~/.var/app/org.chromium.Chromium/**",
    # ... and on Windows, where Chromium-family profiles live under Local and
    # Firefox's (and Opera's) under Roaming.
    "~/AppData/Local/Google/Chrome/**", "~/AppData/Local/Chromium/**",
    "~/AppData/Local/Microsoft/Edge/**", "~/AppData/Local/BraveSoftware/**",
    "~/AppData/Roaming/Mozilla/**", "~/AppData/Roaming/Opera Software/**",
)

#: Where Claude Code keeps every session's transcript and sidecar (`<sid>/tool-results/`),
#: and where the runner leaves each turn's caller envelope, on a laptop.
CLAUDE_PROJECTS = "~/.claude/projects"
CALLER_DIR = "~/.canopy/caller"

#: The names the complement patterns can keep (`complement_globs`): a Claude project
#: directory, a session id, a caller envelope. Anything else is kept by nothing.
_KEEPABLE = re.compile(r"[A-Za-z0-9._-]+")
#: Claude Code's project-directory name: every character but [A-Za-z0-9] becomes '-',
#: and a name over 200 characters is cut there and given a hash we do not compute.
_PROJECT_NAME_MAX = 200


def _glob_class(exclude) -> str:
    """A glob character class matching any one character but `/` and `exclude` (in
    either case: Claude Code matches paths case-insensitively on macOS, so `[#-b]`
    there matches `c` through `C`). Ranges only — a backslash escape inside a class
    breaks the whole rule (measured on Claude Code 2.1.310, as is all of this), while
    a range with `[`, `]` or `^` inside it works. Covers U+0001-U+FFFF, the UTF-16
    surrogates included, which is how a character outside the BMP is matched."""
    skip = {c for e in exclude for c in (e, e.lower(), e.upper())} | {"/"}
    parts: list[tuple[int, int]] = []
    start = prev = None
    for cp in range(0x01, 0x10000):
        if chr(cp) in skip:
            if start is not None:
                parts.append((start, prev))
                start = None
            continue
        if start is None:
            start = cp
        prev = cp
    if start is not None:
        parts.append((start, prev))
    return "[" + "".join(chr(a) if a == b else f"{chr(a)}-{chr(b)}" for a, b in parts) + "]"


def complement_globs(names=(), prefixes=()) -> list[str]:
    """Glob patterns for ONE path component that, together, match every name except
    each of `names` and anything starting with one of `prefixes`.

    Claude Code's deny rules beat its allow rules, and it has no negated pattern
    (`!(x)`, `[!x]` and `[^x]` are all taken literally), so "everything in this
    directory but mine" is spelled as a trie of positive classes: at each character
    of a kept name, every other character. A pattern that matches a directory
    covers everything under it. A name differing from a kept one ONLY in letter case
    is not matched (see `_glob_class`); Claude Code and the runner make every name
    these guard, never a caller. Raises ValueError for a name with a character
    outside `_KEEPABLE`: the caller then denies the directory whole."""
    root: dict = {"c": {}, "end": False, "open": False}
    for name, is_prefix in [(n, False) for n in names] + [(p, True) for p in prefixes]:
        if not _KEEPABLE.fullmatch(name or ""):
            raise ValueError(f"cannot keep {name!r} by pattern")
        node = root
        for ch in name:
            node = node["c"].setdefault(ch, {"c": {}, "end": False, "open": False})
        node["open" if is_prefix else "end"] = True
    out: list[str] = []

    def walk(node: dict, prefix: str) -> None:
        if node["open"]:
            return
        out.append(prefix + _glob_class(set(node["c"])) + "*")
        if prefix and not node["end"]:
            out.append(prefix)
        for ch in sorted(node["c"]):
            walk(node["c"][ch], prefix + ch)

    walk(root, "")
    return out


def _ascii_fold(cp_lo: int, cp_hi: int) -> str:
    """The other-case ASCII letters of the ASCII letters in [cp_lo, cp_hi], as a class
    body: what a case-blind JavaScript regex (`i`, no `u`) adds to the range."""
    return "".join(re.escape(chr(c).swapcase()) for c in range(max(cp_lo, 0x41), min(cp_hi, 0x7A) + 1)
                   if chr(c).isascii() and chr(c).isalpha())


def _glob_regex(glob: str) -> str:
    """One rule's pattern as a Python regex with JavaScript's case-blind matching:
    an ASCII letter matches either case, nothing else is folded."""
    out, i = [], 0
    while i < len(glob):
        ch = glob[i]
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[" and "]" in glob[i + 1:]:
            end = glob.index("]", i + 1)
            body, j, cls = glob[i + 1:end], 0, []
            while j < len(body):
                lo = hi = body[j]
                if j + 2 < len(body) and body[j + 1] == "-":
                    hi, j = body[j + 2], j + 3
                else:
                    j += 1
                cls.append(f"{re.escape(lo)}-{re.escape(hi)}{_ascii_fold(ord(lo), ord(hi))}")
            out.append("(?!/)[" + "".join(cls) + "]")
            i = end + 1
        elif ch.isascii() and ch.isalpha():
            out.append(f"[{ch.lower()}{ch.upper()}]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def denied_by(rules, path: str, *, tool: str = "Read", home: str | None = None) -> list[str]:
    """The `tool(...)` rules in `rules` that match `path` — Claude Code's matching AS
    MEASURED (2.1.310, macOS): `//` anchors an absolute path and `~/` the home, `*`
    stays inside one path component and `**` does not, a class is a positive set of
    characters and ranges, ASCII letters match either case, and a rule that matches a
    directory matches everything under it. A model for tests and for asking "what
    would this deny?"; Claude Code itself decides."""
    home = posix_path(home or str(_home()))
    path = posix_path(path)
    # As JavaScript sees it: a character outside the BMP is two UTF-16 surrogates.
    path = "".join(c if ord(c) <= 0xFFFF else
                   chr(0xD800 + ((ord(c) - 0x10000) >> 10)) + chr(0xDC00 + ((ord(c) - 0x10000) & 0x3FF))
                   for c in path)
    hits = []
    for rule in rules:
        m = re.fullmatch(rf"{re.escape(tool)}\((.*)\)", rule, re.S)
        if not m:
            continue
        pat = m.group(1)
        pat = pat[1:] if pat.startswith("//") else (home + pat[1:] if pat.startswith("~/") else pat)
        rx = re.compile(_glob_regex(pat), re.S)
        parts = path.split("/")
        if any(rx.fullmatch("/".join(parts[:n])) for n in range(2, len(parts) + 1)):
            hits.append(rule)
    return hits


def project_dir_name(cwd: str) -> str:
    """The name of `cwd`'s directory under Claude's `projects/`."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))


def deny_all_but(tool: str, root: str, *, names=(), prefixes=()) -> list[str]:
    """`tool(...)` rules denying everything in `root` but `names` (and names starting
    with a prefix in `prefixes`), by pattern. A name that cannot be kept by pattern
    keeps nothing: the directory is denied whole."""
    try:
        pats = complement_globs(names, prefixes)
    except ValueError:
        pats = ["**"]
    return [_path_rule(tool, f"{root.rstrip('/')}/{p}") for p in pats]


def reach_denies(tool: str, *, cwd: str, caller_path: str | None = None,
                 sessions=None, claude_projects: str = CLAUDE_PROJECTS,
                 caller_dir: str = CALLER_DIR) -> list[str]:
    """What a confined session may never open beside its own: every other directory's
    sessions under `claude_projects`, every session of its own directory but
    `sessions` (when the caller knows them; None keeps the whole directory, which on
    a laptop is the session's own cx- worktree), and every caller envelope but its
    own `caller_path` (none: all of them). By pattern, never a snapshot of what is
    there now (`deny_all_but`)."""
    name = project_dir_name(cwd) if cwd else ""
    if not name:
        rules = [_path_rule(tool, f"{claude_projects.rstrip('/')}/**")]
    elif len(name) > _PROJECT_NAME_MAX:
        rules = deny_all_but(tool, claude_projects, prefixes=[name[:_PROJECT_NAME_MAX] + "-"])
    else:
        rules = deny_all_but(tool, claude_projects, names=[name])
        if sessions is not None:
            rules += deny_all_but(tool, f"{claude_projects.rstrip('/')}/{name}",
                                  names=[s for s in sessions if s])
    own = os.path.basename(caller_path) if caller_path else ""
    if own and os.path.realpath(os.path.dirname(os.path.expanduser(caller_path))) == \
            os.path.realpath(os.path.expanduser(caller_dir)):
        rules += deny_all_but(tool, caller_dir, names=[own])
    else:
        rules.append(_path_rule(tool, f"{caller_dir.rstrip('/')}/**"))
    return rules


def _home() -> pathlib.Path:
    return pathlib.Path.home()


def _load_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _plugin_servers(install_path: pathlib.Path) -> list[str]:
    names: list[str] = []
    doc = _load_json(install_path / ".mcp.json")
    if isinstance(doc, dict):
        block = doc.get("mcpServers", doc)
        if isinstance(block, dict):
            names += [k for k, v in block.items() if isinstance(v, dict)]
    manifest = _load_json(install_path / ".claude-plugin" / "plugin.json")
    block = manifest.get("mcpServers") if isinstance(manifest, dict) else None
    if isinstance(block, dict):
        names += list(block)
    elif isinstance(block, str):
        doc = _load_json(install_path / block)
        if isinstance(doc, dict):
            names += list(doc.get("mcpServers", doc))
    return names


def mcp_server_names(*, home: pathlib.Path | None = None,
                     worktree: str | None = None) -> dict[str, str]:
    """{name as tool names spell it: name as config spells it} for every MCP server a
    session on this box can see — `plugin_ace_ace-ocs` is configured as
    `plugin:ace:ace-ocs`; a user or project server is spelled the same both ways.

    User and project servers from ~/.claude.json, the worktree's `.mcp.json`, and every
    installed plugin's servers. Missing a server costs only its native deny (the hook
    still refuses it); inventing one costs nothing.
    """
    home = home or _home()
    out: dict[str, str] = {}
    cfg = _load_json(home / ".claude.json")
    if isinstance(cfg, dict):
        out |= {k: k for k in (cfg.get("mcpServers") or {})}
        for proj, pcfg in (cfg.get("projects") or {}).items():
            if isinstance(pcfg, dict) and (worktree is None or str(worktree).startswith(str(proj))):
                out |= {k: k for k in (pcfg.get("mcpServers") or {})}
    if worktree:
        doc = _load_json(pathlib.Path(worktree) / ".mcp.json")
        if isinstance(doc, dict):
            out |= {k: k for k in (doc.get("mcpServers") or {})}
    installed = _load_json(home / ".claude" / "plugins" / "installed_plugins.json")
    for key, entries in ((installed or {}).get("plugins") or {}).items():
        plugin = key.split("@", 1)[0]
        for entry in entries if isinstance(entries, list) else []:
            path = (entry or {}).get("installPath")
            if path:
                out |= {f"plugin_{plugin}_{s}": f"plugin:{plugin}:{s}"
                        for s in _plugin_servers(pathlib.Path(path))}
    return {k: v for k, v in out.items() if k and "(" not in k}


def mcp_servers(*, home: pathlib.Path | None = None, worktree: str | None = None) -> set[str]:
    """The MCP server names a session on this box can see, as tool names spell them."""
    return set(mcp_server_names(home=home, worktree=worktree))


def _mcp_split(pattern: str):
    """`mcp__<server-pattern>__<tool-pattern>` → (server-pattern, tool-pattern), or None
    for a pattern that is not server-shaped (`*`, `mcp__*`)."""
    if not pattern.startswith("mcp__"):
        return None
    head, sep, tool = pattern[len("mcp__"):].rpartition("__")
    return (head, tool) if sep and head else None


def _servers_granted(tools: list[str], servers: set[str]) -> tuple[set[str], list[str]]:
    """(servers some tool pattern reaches, allow rules normalised to real servers)."""
    reached: set[str] = set()
    allow: list[str] = []
    for pat in tools:
        split = _mcp_split(pat)
        if split is None:
            if pat.startswith("mcp__") or pat in ("*", "mcp*"):
                # A pattern that is all wildcard reaches every server.
                hit = {s for s in servers if fnmatch.fnmatchcase(f"mcp__{s}__x", pat)}
                reached |= hit
                allow += [f"mcp__{s}__*" for s in sorted(hit)]
            continue
        srv, tool = split
        for s in sorted(servers):
            if fnmatch.fnmatchcase(s, srv):
                reached.add(s)
                allow.append(f"mcp__{s}__{tool}")
    return reached, allow


def _subst(pattern: str, *, cwd: str, thread_id: str) -> str:
    return pattern.replace("{thread_id}", thread_id or "no-thread").replace("{cwd}", cwd)


def _program(pattern: str) -> str:
    try:
        toks = shlex.split(pattern)
    except ValueError:
        toks = pattern.split()
    return os.path.basename(toks[0]) if toks else ""


def posix_path(path: str) -> str:
    """`path` as Claude Code matches it. On Windows it normalizes a path to POSIX form
    before matching a rule — `C:\\Users\\a` is `/c/Users/a` — so a rule written with
    the drive path never matched, and a confined session's allow AND deny rules were
    both inert there (found by the Windows CI, canopy-web#2202). Anything that is not
    a Windows drive path is returned as is."""
    if path.startswith("\\\\?\\"):  # the long-path prefix realpath can return
        path = path[4:]
    m = re.match(r"([A-Za-z]):[\\/]", path)
    if not m:
        return path
    return f"/{m.group(1).lower()}/{path[3:].replace(chr(92), "/")}"  # chr(92) = backslash


def _path_rule(tool: str, path: str) -> str:
    # `//abs` is absolute; `~/…` is home. A bare `/x` would anchor at the settings file.
    path = posix_path(path)
    if path.startswith("/"):
        return f"{tool}(/{path})"
    return f"{tool}({path})"


def settings_for(cap: dict, *, worktree: str, caller_path: str | None = None,
                 thread_id: str = "", servers: set[str] | None = None,
                 sessions=None, claude_projects: str = CLAUDE_PROJECTS,
                 caller_dir: str = CALLER_DIR) -> dict:
    """The `permissions` block for a confined session. Pure (`reach_denies` reads
    nothing but the worktree's real path). `sessions`, `claude_projects` and
    `caller_dir` are `reach_denies`'s."""
    cap = cap or {}
    tools = [str(t) for t in cap.get("tools") or []]
    bash = [str(b) for b in cap.get("bash") or []]
    read_paths = [str(p) for p in cap.get("read_paths") or []]
    write_paths = [str(p) for p in cap.get("write_paths") or []]
    servers = set(servers or ())
    cwd = os.path.realpath(worktree) if worktree else ""

    def granted(tool: str) -> bool:
        if tool == "Bash":
            return bool(bash)            # the guard decides Bash on `bash` alone
        return any(fnmatch.fnmatchcase(tool, p) for p in tools)

    deny: list[str] = [t for t in GATED_TOOLS if not granted(t)]
    allow: list[str] = []

    # Read/Glob/Grep are scoped by read_paths — or, with none, to the session's own
    # working directory, never granted whole (a whole-disk Read reaches every other
    # session's transcript on the box) — and the file-writing tools by write_paths
    # (below); anything else is granted whole.
    reads = ("Read", "Glob", "Grep")
    scoped = set(WRITE_TOOLS) | set(reads)
    allow += [t for t in ("Read",) + GATED_TOOLS
              if t != "Bash" and granted(t) and t not in scoped]
    if not read_paths and cwd and any(granted(t) for t in reads):
        read_paths = ["{cwd}/**"]
    if bash:
        allow += [f"Bash({_subst(b, cwd=cwd, thread_id=thread_id)})" for b in bash]
        mine = {_program(b) for b in bash}
        deny += [f"Bash({p} *)" for p in HIGH_RISK_PROGRAMS if p not in mine]

    # Reads: grant exactly the read_paths, and the caller's own envelope. A Read(path)
    # rule is what Claude Code checks Grep and Glob against too.
    if granted("Read"):
        allow += [_path_rule("Read", _subst(rp, cwd=cwd, thread_id=thread_id)) for rp in read_paths]
    if caller_path:
        allow.append(_path_rule("Read", os.path.realpath(caller_path)))

    # Writes: `Edit(path)` is the one form Claude Code matches for every file-writing
    # tool. A granted write tool lands only inside write_paths; with none, there is
    # nowhere for it to land, so it goes the way of an ungranted tool.
    writers = [t for t in WRITE_TOOLS if granted(t)]
    if writers and write_paths:
        allow += [_path_rule("Edit", _subst(wp, cwd=cwd, thread_id=thread_id)) for wp in write_paths]
    elif writers:
        deny += [t for t in writers if t not in deny]

    # Credential stores, and every other session's and turn's files, taken away from
    # whatever path tool remains.
    reach = dict(cwd=cwd, caller_path=caller_path, sessions=sessions,
                 claude_projects=claude_projects, caller_dir=caller_dir)
    if all(rp.startswith("{cwd}") for rp in read_paths):
        deny += [_path_rule("Read", p) for p in SENSITIVE_PATHS]
        deny += reach_denies("Read", **reach)
    if all(wp.startswith("{cwd}") for wp in write_paths):
        deny += [_path_rule("Edit", p) for p in SENSITIVE_PATHS]
        deny += reach_denies("Edit", **reach)

    mcp_pats = [t for t in tools if t.startswith("mcp") or t == "*"]
    if not mcp_pats:
        deny.append("mcp__*")
    else:
        reached, mcp_allow = _servers_granted(mcp_pats, servers)
        allow += mcp_allow
        deny += [f"mcp__{s}" for s in sorted(servers - reached)]

    return {"defaultMode": "dontAsk", "allow": list(dict.fromkeys(allow)),
            "deny": list(dict.fromkeys(deny))}


def servers_to_stop(cap: dict, names: dict[str, str]) -> list[str]:
    """CONFIG names of the servers this capability never reaches — the ones
    `settings_for` denies as `mcp__<server>` — for `deniedMcpServers`. Pure."""
    tools = [str(t) for t in (cap or {}).get("tools") or []]
    mcp_pats = [t for t in tools if t.startswith("mcp") or t == "*"]
    reached = _servers_granted(mcp_pats, set(names))[0] if mcp_pats else set()
    return sorted(names[s] for s in set(names) - reached)


def _denied_servers_doc(stop: list[str]) -> list[dict]:
    return [{"serverName": n} for n in stop]


#: Written beside our block so a later write replaces OUR rules, not a repo's own.
MARKER_KEY = "canopyConfined"


def write_worktree_settings(worktree: str, permissions: dict, *, capability: str = "",
                            stop_servers: list[str] = ()) -> pathlib.Path:
    """Merge `permissions` (and `stop_servers`, as `deniedMcpServers`) into
    `<worktree>/.claude/settings.json`. Raises OSError.

    A repo may track its own `.claude/settings.json`; its keys are kept, and its
    `deny` rules and `deniedMcpServers` survive (ours are added). Its `allow` rules
    and `defaultMode` do not: a caller's session must not inherit what the agent's own
    sessions are allowed.
    """
    wt = pathlib.Path(worktree)
    if not wt.is_dir():
        raise OSError(f"no worktree at {worktree}")
    path = wt / ".claude" / "settings.json"
    existing = _load_json(path) if path.exists() else {}
    if not isinstance(existing, dict):
        existing = {}
    prior = existing.get("permissions") if isinstance(existing.get("permissions"), dict) else {}
    ours_before = set((existing.get(MARKER_KEY) or {}).get("deny") or [])
    repo_deny = [d for d in prior.get("deny") or [] if d not in ours_before]
    merged = {**prior, "defaultMode": permissions["defaultMode"],
              "allow": list(permissions["allow"]),
              "deny": list(dict.fromkeys(repo_deny + list(permissions["deny"])))}
    ours_stopped = set((existing.get(MARKER_KEY) or {}).get("stopped") or [])
    repo_stopped = [d for d in existing.get("deniedMcpServers") or []
                    if not (isinstance(d, dict) and d.get("serverName") in ours_stopped)]
    doc = {**existing, "permissions": merged,
           MARKER_KEY: {"capability": capability, "deny": list(permissions["deny"]),
                        "stopped": list(stop_servers)}}
    stopped = repo_stopped + _denied_servers_doc(list(stop_servers))
    if stopped:
        doc["deniedMcpServers"] = stopped
    else:
        doc.pop("deniedMcpServers", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".settings.json.canopy-tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path


def confine_worktree(cap: dict, worktree: str, *, caller_path: str | None = None,
                     thread_id: str = "", home: pathlib.Path | None = None) -> pathlib.Path:
    """Compute and write the native layer for one confined worktree. Raises OSError.
    The session's own Claude sessions are not known here (emdash starts them), so
    its whole project directory stays its own (`reach_denies`, sessions=None)."""
    names = mcp_server_names(home=home, worktree=worktree)
    perms = settings_for(cap, worktree=worktree, caller_path=caller_path, thread_id=thread_id,
                         servers=set(names))
    return write_worktree_settings(worktree, perms, capability=str((cap or {}).get("name") or ""),
                                   stop_servers=servers_to_stop(cap, names))


def cli_settings(cap: dict, *, cwd: str, caller_path: str | None = None, thread_id: str = "",
                 home: pathlib.Path | None = None, sessions=None,
                 claude_projects: str = CLAUDE_PROJECTS, caller_dir: str = CALLER_DIR) -> dict:
    """The same rules as a `--settings` document, for a runner that spawns claude itself."""
    names = mcp_server_names(home=home, worktree=cwd)
    perms = settings_for(cap, worktree=cwd, caller_path=caller_path, thread_id=thread_id,
                         servers=set(names), sessions=sessions,
                         claude_projects=claude_projects, caller_dir=caller_dir)
    doc = {"permissions": perms}
    stop = servers_to_stop(cap, names)
    if stop:
        doc["deniedMcpServers"] = _denied_servers_doc(stop)
    return doc
