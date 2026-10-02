"""`canopy-runner pair` — set a NEW macOS account up as a runner, in one step.

Before this existed, adding a runner for another account on the same laptop (the
fleet runs one per macOS account, for Claude-subscription failover) was five hand
steps, and two of them bit the third account on 2026-09-28:

- the README's pairing curl omitted `workspace`, which 422s for anyone in more than
  one workspace ("… you belong to 2 workspaces, so there is no default");
- the account needs its OWN CDP and hook ports — two emdash instances cannot share a
  debug port — which meant reading the sibling accounts' runner.json by hand;
- the "Emdash CDP" launcher the README tells you to open was never created by anything.

What this does, in order: pair (or recognise an existing pairing), choose free
ports, write ~/.canopy/runner.json, and build the per-account launcher. It is
IDEMPOTENT on purpose: install-runner.sh calls it on every hand-run install of a box
with no config, and re-running it on a paired box must never create a second runner.

Stdlib only, like the rest of the runner. Every server call goes through `Client`,
so tests drive it with a fake.
"""
from __future__ import annotations

import json
import os
import platform
import pwd
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path

from .client import Client

DEFAULT_BASE_URL = "https://labs.connect.dimagi.com/canopy"
DEFAULT_TOKEN_REF = "@~/.claude/canopy/workbench-token"
# The first account took the Chrome/Electron default 9222 and the hook listener's
# default 8787; each further account steps up from there (jj 9222/8787, acedimagi
# 9223/8788, haldimagi 9224/8789).
CDP_BASE = 9222
HOOK_BASE = 8787
PORT_SPAN = 100
LAUNCHER_NAME = "Emdash CDP.app"


class PairError(Exception):
    """A pairing step refused to proceed. The message is for a human and says why."""


# --- identity -----------------------------------------------------------------


def macos_user() -> str:
    """The account from the UID, never $LOGNAME/$USER: a session with LOGNAME=root
    once pinned a runner to an account that doesn't exist (#1008)."""
    return pwd.getpwuid(os.getuid()).pw_name


def default_runner_name(user: str) -> str:
    return f"{user}-mbp-cdp"


# --- server-side decisions ----------------------------------------------------


def resolve_workspace(explicit: str, workspaces: list[dict]) -> str:
    """The workspace to pair into. Never guesses between several.

    A runner's workspace only gates who can SEE it (what it may work for follows
    `owner`), but the server refuses to default it for a multi-workspace owner,
    and so do we — with the list, so the fix is one flag away.
    """
    slugs = [str(w.get("slug")) for w in workspaces if w.get("slug")]
    if explicit:
        if explicit not in slugs:
            raise PairError(
                f"you are not a member of workspace '{explicit}' "
                f"(yours: {', '.join(slugs) or 'none'})")
        return explicit
    if len(slugs) == 1:
        return slugs[0]
    if not slugs:
        raise PairError("you belong to no canopy-web workspace — join one first")
    raise PairError(
        f"you belong to {len(slugs)} workspaces ({', '.join(slugs)}), so there is no "
        "default — pass --workspace <slug>")


def default_agents(runners: list[dict], exclude_id: str = "") -> list[str]:
    """The agents your OTHER runners serve — a new account is normally another seat
    for the same fleet. Only runners you manage (owned by you) count: someone else's
    box serving an agent says nothing about what yours should."""
    agents: set[str] = set()
    for r in runners:
        if not r.get("can_manage") or str(r.get("id")) == exclude_id:
            continue
        agents.update(a for a in (r.get("capabilities") or {}).get("agents") or [] if a)
    return sorted(agents)


def _find(runners: list[dict], runner_id: str) -> dict | None:
    return next((r for r in runners if str(r.get("id")) == runner_id), None)


# --- local decisions ----------------------------------------------------------


def sibling_ports(users_root: Path, own_home: Path) -> set[int]:
    """Every cdp_port/hook_port claimed by ANOTHER account's runner.json.

    This is why runner.json is written 0644: the scan runs as the new account and
    reads the others' files. The file holds no secret — the token is a
    `@path` reference to the PAT, which stays 0600 in ~/.claude — so world-readable
    costs nothing. An unreadable or malformed sibling is skipped, not fatal: the
    live-port check below still catches a sibling that is actually running.
    """
    taken: set[int] = set()
    try:
        own = own_home.resolve()
    except OSError:
        own = own_home
    for path in sorted(users_root.glob("*/.canopy/runner.json")):
        try:
            if path.parent.parent.resolve() == own:
                continue
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for key in ("cdp_port", "hook_port"):
            val = raw.get(key)
            if isinstance(val, int) and val > 0:
                taken.add(val)
    return taken


def port_free(port: int) -> bool:
    """Nothing is listening on loopback:port and we could bind it. Both checks,
    because a sibling account's emdash may be listening on a wildcard address."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def emdash_live_cdp_port(home: Path, *, is_up=None) -> int | None:
    """The CDP port THIS account's running emdash already exposes, or None.

    Electron writes `DevToolsActivePort` (first line = port) into its userData dir
    when launched with --remote-debugging-port. Without this, pairing on a box whose
    emdash is already up saw 9222 as "taken" and proposed 9223 — a port nothing
    listens on, so the runner would have been `cdp_down` forever (2026-10-02, a
    re-pair with emdash running). The file outlives the process, so it is trusted
    only while something actually answers DevTools on that port.
    """
    is_up = is_up or cdp_up
    support = home / "Library" / "Application Support"
    try:
        names = sorted(os.listdir(support))
    except OSError:
        return None
    for name in (n for n in names if n.lower() == "emdash"):
        try:
            first = (support / name / "DevToolsActivePort").read_text().splitlines()[0]
            port = int(first.strip())
        except (OSError, ValueError, IndexError):
            continue
        if 0 < port < 65536 and is_up(port):
            return port
    return None


def choose_port(base: int, taken: set[int], is_free=port_free) -> int:
    for port in range(base, base + PORT_SPAN):
        if port not in taken and is_free(port):
            return port
    raise PairError(f"no free port in {base}..{base + PORT_SPAN - 1}")


def find_emdash_db(home: Path) -> Path:
    """emdash's sqlite path, with the directory's REAL case.

    Older installs used `Emdash`, newer ones `emdash` (the fleet has both). macOS's
    default filesystem is case-insensitive, so `exists()` says yes to either spelling
    — only a directory listing reveals which one is actually there.
    """
    support = home / "Library" / "Application Support"
    try:
        names = sorted(os.listdir(support))
    except OSError:
        names = []
    candidates = [n for n in names if n.lower() == "emdash"]
    for name in candidates:
        if (support / name / "emdash4.db").is_file():
            return support / name / "emdash4.db"
    return support / (candidates[0] if candidates else "emdash") / "emdash4.db"


def build_config(*, base_url: str, token_ref: str, runner_id: str, emdash_db: Path,
                 cdp_port: int, hook_port: int) -> dict:
    """The fields README step 3 documents; everything else keeps its Config default."""
    return {
        "base_url": base_url,
        "token": token_ref,
        "runner_id": runner_id,
        "emdash_db": str(emdash_db),
        "cdp_port": cdp_port,
        "poll_seconds": 5,
        "inbox_poll_seconds": 300,
        "mailboxes": {},
        "hook_port": hook_port,
        "forward_sessions": True,
    }


def write_config(path: Path, data: dict) -> None:
    """Create runner.json — never replace one (the caller checked it is absent).
    0644, see sibling_ports for why that is both needed and safe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o644)
    if path.exists():
        tmp.unlink()
        raise PairError(f"{path} appeared while pairing — not overwriting it")
    os.replace(tmp, path)


# --- the launcher -------------------------------------------------------------


def _emdash_app(home: Path) -> Path:
    for cand in (Path("/Applications/Emdash.app"), home / "Applications" / "Emdash.app"):
        if cand.exists():
            return cand
    return Path("/Applications/Emdash.app")


def launcher_script(port: int, emdash_app: Path) -> list[str]:
    """AppleScript for a launcher that restarts emdash on THIS account's CDP port.

    It QUITS a running emdash first: the debug port is a launch argument, so there
    is no attaching one to a running instance — and quitting kills every Claude
    session running inside emdash."""
    return [
        'tell application "Emdash" to if it is running then quit',
        "delay 2",
        f'do shell script "open -na \\"{emdash_app}\\" --args --remote-debugging-port={port}"',
    ]


def ensure_launcher(port: int, home: Path, *, run=subprocess.run,
                    system: str | None = None) -> str:
    """Build ~/Applications/Emdash CDP.app for `port` (Spotlight: "Emdash CDP").

    Best-effort and macOS-only: returns what happened, never raises. Compiling the
    app never RUNS it, so this cannot disturb a running emdash."""
    if (system or platform.system()) != "Darwin":
        return "skipped (not macOS)"
    if not shutil.which("osacompile"):
        return "skipped (osacompile not found)"
    app = home / "Applications" / LAUNCHER_NAME
    flag = f"--remote-debugging-port={port}"
    if app.exists():
        try:
            out = run(["osadecompile", str(app / "Contents/Resources/Scripts/main.scpt")],
                      capture_output=True, text=True, timeout=30)
            if out.returncode == 0 and flag in out.stdout:
                return f"unchanged ({app})"
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        app.parent.mkdir(parents=True, exist_ok=True)
        existed = app.exists()
        if existed:
            shutil.rmtree(app)
        cmd = ["osacompile", "-o", str(app)]
        for line in launcher_script(port, _emdash_app(home)):
            cmd += ["-e", line]
        res = run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            return f"FAILED ({(res.stderr or '').strip()[:200]})"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"FAILED ({exc})"
    return f"{'updated' if existed else 'created'} ({app})"


def cdp_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
            return True
    except Exception:  # noqa: BLE001 — any failure means "not reachable"
        return False


# --- orchestration ------------------------------------------------------------


def _token_from_ref(ref: str) -> str:
    if not ref.startswith("@"):
        return ref
    path = Path(ref[1:]).expanduser()
    try:
        tok = path.read_text().strip()
    except OSError:
        tok = ""
    if not tok:
        raise PairError(
            f"no canopy-web PAT at {path} — mint one first (the canopy:canopy-web-pat-mint "
            "skill), then re-run")
    return tok


def _list_runners(client) -> list[dict]:
    _, rows = client._call("GET", "/runners/")
    return list(rows or [])


def run_pair(config_path: Path, *, name: str = "", workspace: str = "",
             agents: list[str] | None = None, cdp_port: int = 0, hook_port: int = 0,
             runner_id: str = "", base_url: str = DEFAULT_BASE_URL,
             token_ref: str = DEFAULT_TOKEN_REF, home: Path | None = None,
             users_root: Path = Path("/Users"), launcher: bool = True,
             dry_run: bool = False, client_factory=Client, is_free=port_free,
             ensure_launcher_fn=ensure_launcher, live_cdp_port=emdash_live_cdp_port,
             out=print) -> int:
    """Pair this account (or confirm it already is) and write its runner.json.

    Returns 0 on success, including "already paired". Raises PairError to refuse.
    """
    home = home or Path.home()
    config_path = Path(config_path).expanduser()

    # --- already configured: verify, never re-pair ---------------------------
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text())
        except (OSError, ValueError) as exc:
            raise PairError(f"{config_path} is unreadable ({exc}) — fix or move it aside")
        rid = str(raw.get("runner_id") or "")
        if not rid:
            raise PairError(f"{config_path} has no runner_id — fix or move it aside")
        client = client_factory(raw.get("base_url") or base_url,
                                _token_from_ref(str(raw.get("token") or token_ref)))
        row = _find(_list_runners(client), rid)
        if row is None:
            # Not listed = retired, or owned by someone this PAT can't see. Pairing
            # a fresh one here would silently orphan whatever that runner was for.
            raise PairError(
                f"{config_path} names runner {rid}, which canopy-web does not list for "
                "you (retired, or owned by another user). Not pairing a second one "
                f"automatically: unretire it (POST /api/harness/runners/{rid}/unretire), "
                "or move the file aside and re-run.")
        out(f"==> already paired: {row.get('name')} ({rid}) — not pairing again")
        port = int(raw.get("cdp_port") or CDP_BASE)
        if launcher and not dry_run:
            out(f"==> Emdash CDP launcher: {ensure_launcher_fn(port, home)}")
        return 0

    # --- fresh account ----------------------------------------------------------
    client = client_factory(base_url, _token_from_ref(token_ref))
    _, workspaces = client._call_api("/workspaces/", method="GET")
    ws = resolve_workspace(workspace, list(workspaces or []))
    runners = _list_runners(client)

    if runner_id:
        row = _find(runners, runner_id)
        if row is None or not row.get("can_manage"):
            raise PairError(f"runner {runner_id} is not one you manage — cannot adopt it")
        name = str(row.get("name") or name)
    else:
        name = name or default_runner_name(macos_user())
        clash = next((r for r in runners if r.get("name") == name), None)
        if clash is not None and not clash.get("can_manage"):
            # Re-pairing under a NEW owner (e.g. a box first paired with the wrong
            # token). Adoption can't help — it never transfers ownership — so say
            # what does (2026-10-02).
            raise PairError(
                f"a runner named '{name}' already exists ({clash.get('id')}) and it is "
                f"not yours (owned by {clash.get('owner_email') or 'someone else'}), "
                "so it can't be adopted — --runner-id never transfers ownership. Pick "
                "another --name, or have its owner retire it first "
                f"(POST /api/harness/runners/{clash.get('id')}/retire); retired runners "
                "don't count.")
        if clash is not None:
            raise PairError(
                f"a runner named '{name}' already exists ({clash.get('id')}). If it is "
                f"this account's, adopt it with --runner-id {clash.get('id')}; "
                "otherwise pick another --name.")

    if runner_id:
        agents = []  # adopting: the runner keeps whatever it already serves
    else:
        agents = list(agents or default_agents(runners))
        if not agents:
            raise PairError("none of your runners serve an agent to copy — pass --agents a,b,c")

    taken = sibling_ports(users_root, home)
    if cdp_port:
        cdp, cdp_source = cdp_port, "--cdp-port"
    else:
        live = live_cdp_port(home)
        if live is not None and live not in taken:
            # emdash on THIS account is already serving DevTools here: the runner must
            # drive that port, not the next free one (which nothing would listen on).
            cdp, cdp_source = live, "this account's running emdash (DevToolsActivePort)"
        else:
            if live is not None:
                out(f"    (emdash's DevTools port {live} is claimed by another account's "
                    "runner.json — choosing a fresh one; relaunch emdash with "
                    "'Emdash CDP' after pairing)")
            cdp, cdp_source = choose_port(CDP_BASE, taken, is_free), "first free port"
    hook = hook_port or choose_port(HOOK_BASE, taken | {cdp}, is_free)
    db = find_emdash_db(home)

    out(f"==> pairing '{name}' | workspace {ws} | agents {','.join(agents) or '(unchanged)'}")
    out(f"    cdp_port {cdp} (from {cdp_source}) | hook_port {hook} | emdash_db {db}")
    if not db.exists():
        out("    (no emdash db there yet — fine if emdash has never run on this account)")
    if dry_run:
        out("==> dry run: nothing paired, nothing written")
        return 0

    if not runner_id:
        # retry=False: POST /runners/ is not idempotent — a re-send after a reply
        # lost in transit would pair a duplicate.
        _, created = client._call("POST", "/runners/", {
            "name": name, "kind": "emdash", "workspace": ws,
            # `sessions: true` — a laptop emdash runner takes Slack/chat (session)
            # turns; without it the box pairs looking healthy and every chat turn
            # for its agents sits UNROUTED (2026-10-02, stewari-mbp-cdp: "no runner
            # is set up to run ace", which reads like a routing/identity fault).
            "capabilities": {"agents": agents, "sessions": True},
        }, retry=False)
        runner_id = str((created or {}).get("id") or "")
        if not runner_id:
            raise PairError(f"pairing returned no runner id: {created!r}")
        out(f"==> paired {name} -> {runner_id}")
    else:
        out(f"==> adopting existing runner {name} ({runner_id})")

    write_config(config_path, build_config(
        base_url=base_url, token_ref=token_ref, runner_id=runner_id, emdash_db=db,
        cdp_port=cdp, hook_port=hook))
    out(f"==> wrote {config_path}")
    if launcher:
        out(f"==> Emdash CDP launcher: {ensure_launcher_fn(cdp, home)}")
    return 0
