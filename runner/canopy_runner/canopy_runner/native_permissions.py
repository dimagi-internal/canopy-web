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
#: (as `Edit(…)`) when every write_path is.
SENSITIVE_PATHS = (
    "~/.ssh/**", "~/.aws/**", "~/.gnupg/**", "~/.config/gh/**", "~/.config/op/**",
    "~/.docker/**", "~/.netrc", "~/.claude.json", "~/.claude/canopy/**",
    "~/.canopy/profiles/**", "~/.canopy/caller/pending/**", "~/.canopy/runner.json",
)


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


def _path_rule(tool: str, path: str) -> str:
    # `//abs` is absolute; `~/…` is home. A bare `/x` would anchor at the settings file.
    if path.startswith("/"):
        return f"{tool}(/{path})"
    return f"{tool}({path})"


def settings_for(cap: dict, *, worktree: str, caller_path: str | None = None,
                 thread_id: str = "", servers: set[str] | None = None) -> dict:
    """The `permissions` block for a confined session. Pure."""
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

    # Read/Glob/Grep are scoped by read_paths, the file-writing tools by write_paths
    # (below); anything else is granted whole.
    scoped = set(WRITE_TOOLS) | ({"Read", "Glob", "Grep"} if read_paths else set())
    allow += [t for t in ("Read",) + GATED_TOOLS
              if t != "Bash" and granted(t) and t not in scoped]
    if bash:
        allow += [f"Bash({_subst(b, cwd=cwd, thread_id=thread_id)})" for b in bash]
        mine = {_program(b) for b in bash}
        deny += [f"Bash({p} *)" for p in HIGH_RISK_PROGRAMS if p not in mine]

    # Reads: grant exactly the read_paths, and the caller's own envelope.
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

    # Credential stores, taken away from whatever path tool remains.
    if all(rp.startswith("{cwd}") for rp in read_paths):
        deny += [_path_rule("Read", p) for p in SENSITIVE_PATHS]
    if all(wp.startswith("{cwd}") for wp in write_paths):
        deny += [_path_rule("Edit", p) for p in SENSITIVE_PATHS]

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
    """Compute and write the native layer for one confined worktree. Raises OSError."""
    names = mcp_server_names(home=home, worktree=worktree)
    perms = settings_for(cap, worktree=worktree, caller_path=caller_path, thread_id=thread_id,
                         servers=set(names))
    return write_worktree_settings(worktree, perms, capability=str((cap or {}).get("name") or ""),
                                   stop_servers=servers_to_stop(cap, names))


def cli_settings(cap: dict, *, cwd: str, caller_path: str | None = None, thread_id: str = "",
                 home: pathlib.Path | None = None) -> dict:
    """The same rules as a `--settings` document, for a runner that spawns claude itself."""
    names = mcp_server_names(home=home, worktree=cwd)
    perms = settings_for(cap, worktree=cwd, caller_path=caller_path, thread_id=thread_id,
                         servers=set(names))
    doc = {"permissions": perms}
    stop = servers_to_stop(cap, names)
    if stop:
        doc["deniedMcpServers"] = _denied_servers_doc(stop)
    return doc
