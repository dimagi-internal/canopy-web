"""The slash commands a Claude session on this box can run, sent to canopy-web for
the chat composer's `/` autocomplete.

Read from disk, the way Claude Code finds them: the account's own skills and
commands (`~/.claude/skills/*/SKILL.md`, `~/.claude/commands/*.md`) and those of
every ENABLED plugin (`installed_plugins.json` ∩ `settings.json` enabledPlugins),
named `plugin:command` like Claude Code names them. Commands a repo defines in its
own `.claude/` are not read: emdash does not record where a project lives.

Built-ins (`/compact`, `/model`, …) are not offered from here. Typed into
emdash's TUI, run by the desktop app or passed to `claude -p`, each one behaves
differently and several do nothing, so none is offered. An ACP executor's own
list (`available_commands_update`) is the exception, because Claude names what it
supports there (`remember_acp`, merged into every report).

Pure stdlib, shared with the cloud runner. Sent only when it changes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import re
import time

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 300
MAX_COMMANDS = 1000
MAX_DESCRIPTION = 300
MAX_ARGS_HINT = 120

_state = {"at": 0.0, "sent": ""}
#: What ACP sessions on this box announced, by name — the latest wins.
_acp_seen: dict[str, dict] = {}

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")


def _load_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _frontmatter(text: str) -> tuple[dict, str]:
    """(top-level scalar keys, body). Enough YAML for a skill's header: `key: value`,
    quoted values, and `|`/`>` blocks folded to one line."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head, body = text[3:end], text[end + 4:]
    out: dict[str, str] = {}
    key = None
    block: list[str] = []
    for line in head.splitlines():
        if key is not None:
            if line.startswith((" ", "\t")) or not line.strip():
                block.append(line.strip())
                continue
            out[key] = " ".join(b for b in block if b)
            key = None
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if v in ("|", ">", "|-", ">-"):
            key, block = k, []
            continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1].replace('\\"', '"') if v[0] == '"' else v[1:-1].replace("''", "'")
        out[k] = v
    if key is not None:
        out[key] = " ".join(b for b in block if b)
    return out, body


def _entry(path: pathlib.Path, prefix: str, default: str, source: str, *,
           named_by_header: bool) -> dict | None:
    """One command from its file. A skill may name itself in its header; a
    command file is named by its file name."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    meta, body = _frontmatter(text)
    if str(meta.get("user-invocable", "")).lower() == "false":
        return None
    name = prefix + ((meta.get("name") if named_by_header else "") or default)
    if not _NAME.match(name):
        return None
    description = meta.get("description", "")
    if not description:
        description = next((ln.strip().lstrip("#").strip() for ln in body.splitlines() if ln.strip()), "")
    return {
        "name": name,
        "description": description[:MAX_DESCRIPTION],
        "args_hint": str(meta.get("argument-hint", ""))[:MAX_ARGS_HINT],
        "source": source,
    }


def _scan_dir(root: pathlib.Path, prefix: str, source: str) -> list[dict]:
    """`<root>/skills/<name>/SKILL.md` and `<root>/commands/<name>.md`."""
    found = []
    skills = root / "skills"
    if skills.is_dir():
        found += [_entry(d / "SKILL.md", prefix, d.name, source, named_by_header=True)
                  for d in sorted(skills.iterdir()) if (d / "SKILL.md").is_file()]
    commands = root / "commands"
    if commands.is_dir():
        found += [_entry(md, prefix, md.stem, source, named_by_header=False)
                  for md in sorted(commands.rglob("*.md"))]
    return [e for e in found if e]


def _enabled_plugins(home: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    installed = _load_json(home / ".claude" / "plugins" / "installed_plugins.json") or {}
    settings = _load_json(home / ".claude" / "settings.json") or {}
    enabled = settings.get("enabledPlugins") if isinstance(settings, dict) else None
    enabled = enabled if isinstance(enabled, dict) else {}
    out = []
    for key, entries in (installed.get("plugins") or {}).items():
        if enabled.get(key) is False:
            continue
        for entry in entries if isinstance(entries, list) else []:
            path = (entry or {}).get("installPath") if isinstance(entry, dict) else None
            if not path:
                continue
            root = pathlib.Path(path)
            manifest = _load_json(root / ".claude-plugin" / "plugin.json") or {}
            name = manifest.get("name") if isinstance(manifest, dict) else None
            out.append((name or key.split("@", 1)[0], root))
            break
    return out


def _acp_entries(available: list) -> list[dict]:
    """ACP's `availableCommands` in the reported shape."""
    out = []
    for c in available or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").lstrip("/")
        if not _NAME.match(name):
            continue
        hint = (c.get("input") or {}).get("hint", "") if isinstance(c.get("input"), dict) else ""
        out.append({"name": name, "description": str(c.get("description") or "")[:MAX_DESCRIPTION],
                    "args_hint": str(hint or "")[:MAX_ARGS_HINT], "source": "acp"})
    return out


def remember_acp(available) -> None:
    """Keep an ACP session's `availableCommands` for the next report. Never raises:
    an executor calls it on its way out of a turn."""
    try:
        for entry in _acp_entries(available):
            _acp_seen[entry["name"]] = entry
    except Exception:  # noqa: BLE001
        logger.debug("unreadable availableCommands", exc_info=True)


def scan(home: pathlib.Path | None = None) -> list[dict]:
    """Every command a session on this box can run, by name, sorted. On a name
    clash the first wins: what ACP announced (Claude's own list), then the
    account's own, then plugins."""
    home = home or pathlib.Path.home()
    found: list[dict] = [_acp_seen[k] for k in sorted(_acp_seen)]
    found += _scan_dir(home / ".claude", "", "user")
    for plugin, root in _enabled_plugins(home):
        found += _scan_dir(root, f"{plugin}:", "plugin")
    by_name: dict[str, dict] = {}
    for e in found:
        by_name.setdefault(e["name"], e)
    return [by_name[k] for k in sorted(by_name)][:MAX_COMMANDS]


def digest(commands: list[dict]) -> str:
    return hashlib.sha256(json.dumps(commands, sort_keys=True).encode()).hexdigest()


def maybe_report(send, *, home: pathlib.Path | None = None, now_fn=time.time) -> bool:
    """Scan every INTERVAL_SECONDS and `send(commands)` when the list changed since
    the last send. Never raises; a failed send is retried at the next scan."""
    now = now_fn()
    if now < _state["at"]:
        return False
    _state["at"] = now + INTERVAL_SECONDS
    try:
        commands = scan(home)
        h = digest(commands)
        if h == _state["sent"]:
            return False
        send(commands)
    except Exception:  # noqa: BLE001 — a report never breaks the tick
        logger.warning("slash command report failed; will retry", exc_info=True)
        return False
    _state["sent"] = h
    logger.info("slash commands: reported %d", len(commands))
    return True
