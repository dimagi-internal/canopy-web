"""`canopy-runner app …`: what the menu bar and a person's terminal call.

Refusals print on stdout with exit 1, because the menu bar reads stdout and shows the
words. Exit 2 stays argparse's, which the menu bar reads as "needs a newer runner".
`status` and `stop` never touch the network: stopping an app must work offline.
"""
from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from . import package, server, state
from .host import ChildSpec, RunSpec
from .manifest import PYTHON

CHECK_SECONDS = 10
STOP_WAIT_SECONDS = 8
HOST_START_WAIT_SECONDS = 5
SENSITIVE_STREAMS = {"transcript"}
HOST_MODULE = "canopy_runner.local_apps.host"
KILLED_BY_STOP = "killed_by_stop_command"


class Refusal(Exception):
    """Why this can't go ahead, in words for the person."""


def current_platform() -> str:
    return "macos" if sys.platform == "darwin" else sys.platform


@dataclasses.dataclass
class Plan:
    key: str
    version: str
    manifest: dict
    cwd: Path
    settings: dict
    unpublished: bool
    source: Optional["Plan"] = None
    needs_acceptance: bool = False
    changes: list = dataclasses.field(default_factory=list)


# --- talking to the server ---------------------------------------------------------


def _server(args) -> server.RunnerServer:
    # Imported here so `status`, `stop` and dev-installed apps never load a credential.
    from ..client import Client
    from ..config import Config
    from ..runner_token import RunnerCredential

    try:
        cfg = Config.load(Path(args.config))
    except (OSError, ValueError, TypeError) as exc:
        raise Refusal(f"this computer isn't set up as a runner ({exc})") from exc
    return server.RunnerServer(Client(cfg.base_url, RunnerCredential.for_config(cfg)), cfg.runner_id)


def _ensure_unpacked(entry: dict, srv: server.RunnerServer) -> Path:
    """The version the server named, on disk. Only its sha256 and size are believed, and
    a failed check leaves whatever was there untouched."""
    key, version = entry.get("key"), entry.get("version")
    target = state.version_dir(key, version)
    want = str(entry.get("sha256") or "").lower()
    stamp = target.with_name(f"{version}.sha256")
    if not want:
        raise Refusal(f"canopy named no sha256 for {key} {version}")
    if target.is_dir() and stamp.exists() and stamp.read_text().strip() == want:
        return target
    data = srv.package(key, version)
    try:
        size = int(entry.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if len(data) != size:
        raise Refusal(f"the download of {key} is {len(data)} bytes, not the {size} canopy announced")
    if package.sha256(data) != want:
        raise Refusal(f"the download of {key} doesn't match canopy's sha256; refusing it")
    state.ensure_dir(key)
    package.unpack(data, target)
    stamp.write_text(want + "\n")
    return target


def _published_plan(entry: dict, by_key: dict, srv: server.RunnerServer) -> Plan:
    cwd = _ensure_unpacked(entry, srv)
    manifest = package.read_manifest(cwd)
    if manifest["key"] != entry.get("key") or manifest["version"] != entry.get("version"):
        raise Refusal(f"the package for {entry.get('key')} says it is "
                      f"{manifest['key']} {manifest['version']}; refusing it")
    plan = Plan(key=manifest["key"], version=manifest["version"], manifest=manifest, cwd=cwd,
                settings=entry.get("settings") or {}, unpublished=False,
                needs_acceptance=bool(entry.get("needs_acceptance")),
                changes=list(entry.get("changes") or []))
    source_key = entry.get("source_key")
    if source_key:
        source = by_key.get(source_key)
        if source is None:
            raise Refusal(f"{entry.get('key')} needs {source_key}, which isn't installed on this computer")
        if source.get("refusal"):
            raise Refusal(f"{source_key}: {source['refusal']}")
        plan.source = _published_plan(source, by_key, srv)
    elif manifest["consumes"]:
        raise Refusal(f"{manifest['key']} needs a source of {', '.join(manifest['consumes'])}, "
                      "and none is installed on this computer")
    return plan


def _dev_entries() -> dict:
    out = {}
    for key in state.installed_keys():
        record = state.read_json(state.dev_file(key))
        if record.get("path"):
            out[key] = record
    return out


def _dev_plan(key: str, record: dict, *, with_source: bool = True) -> Plan:
    folder = Path(record["path"])
    manifest = package.read_manifest(folder)
    if manifest["key"] != key:
        raise Refusal(f"{folder} now holds {manifest['key']}, not {key}. Run dev-install again.")
    settings = record.get("settings") or {}
    # An agent is the server's business, and a dev run has no server.
    missing = [name for name, spec in manifest["settings"].items()
               if spec.get("required") and spec.get("type") != "agent" and name not in settings]
    if missing:
        raise Refusal(f"{key} needs settings: {', '.join(missing)}. Run dev-install again with --set.")
    plan = Plan(key=key, version=manifest["version"], manifest=manifest, cwd=folder,
                settings=settings, unpublished=True)
    if with_source and manifest["consumes"]:
        for other, other_record in sorted(_dev_entries().items()):
            if other == key:
                continue
            candidate = _dev_plan(other, other_record, with_source=False)
            if set(candidate.manifest["provides"]) & set(manifest["consumes"]):
                plan.source = candidate
                break
        else:
            raise Refusal(f"{key} needs a source of {', '.join(manifest['consumes'])}. "
                          "dev-install one first: a dev run uses no server, so it can't use a published one.")
    return plan


def _plan(key: str, args) -> tuple[Plan, Optional[server.RunnerServer]]:
    dev = _dev_entries()
    if key in dev:
        return _dev_plan(key, dev[key]), None
    srv = _server(args)
    by_key = {str(a.get("key")): a for a in srv.list_apps()}
    entry = by_key.get(key)
    if entry is None:
        raise Refusal(f"{key} isn't installed on this computer. Install it from the app store.")
    if entry.get("refusal"):
        raise Refusal(str(entry["refusal"]))
    return _published_plan(entry, by_key, srv), srv


def _refuse_wrong_platform(plan: Plan) -> None:
    for each in filter(None, (plan, plan.source)):
        if current_platform() not in each.manifest["platforms"]:
            raise Refusal(f"{each.key} doesn't run on {current_platform()}")
    if plan.manifest["provides"]:
        raise Refusal(f"{plan.key} is a source: it starts with an app that uses it, not by itself")


# --- checks ------------------------------------------------------------------------


def _run_check(check: dict, cwd: Path) -> bool:
    argv = [sys.executable if i == 0 and part == PYTHON else part for i, part in enumerate(check["run"])]
    # Only the exit code is used: a check's output could quote anything it read.
    try:
        return subprocess.run(argv, cwd=cwd, env=state.scrubbed_env(), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=CHECK_SECONDS).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run_checks(plan: Plan) -> list:
    results = []
    for each in filter(None, (plan.source, plan)):
        for check in each.manifest["checks"]:
            results.append({"app": each.key, "label": check["label"], "fix": check["fix"],
                            "ok": _run_check(check, each.cwd)})
    return results


def _print_failed(failed: list) -> None:
    for item in failed:
        print(f"  not ready: {item['label']}\n    to fix: {item['fix']}")


# --- starting ----------------------------------------------------------------------


def _notice_text(plan: Plan) -> str:
    lines = []
    for each in filter(None, (plan, plan.source)):
        manifest = each.manifest
        if manifest["notice"]:
            lines.append(manifest["notice"])
        lines.extend(f"Reads: {text}" for text in manifest["disclosure"]["reads"])
        lines.extend(f"Sends: {text}" for text in manifest["disclosure"]["sends"])
    return "\n".join(lines)


def _confirm_notice(plan: Plan, accepted: bool) -> None:
    text = _notice_text(plan)
    if accepted or not text:
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise Refusal(f"{text}\n\nPass --accept-notice once the person has read this.")
    print(text)
    if input("Start? [y/N] ").strip().lower() not in ("y", "yes"):
        raise Refusal("not started")


def _child_spec(plan: Plan) -> ChildSpec:
    return ChildSpec(key=plan.key, version=plan.version, entry=list(plan.manifest["entry"]),
                     cwd=str(plan.cwd), settings=dict(plan.settings))


def _is_sensitive(plan: Plan) -> bool:
    streams = set(plan.manifest["consumes"]) | set(plan.manifest["provides"])
    if plan.source:
        streams |= set(plan.source.manifest["provides"])
    return bool(streams & SENSITIVE_STREAMS)


def spawn_host(spec: RunSpec) -> subprocess.Popen:
    """Detached, with no pipe shared with this command: a caller reads this command's
    stdout to its end, and a host holding it open would hang that caller for the whole run."""
    state.ensure_dir(spec.key)
    path = state.spec_file(spec.key)
    state.write_json(path, spec.to_json())
    error_fd = os.open(state.app_dir(spec.key) / "host.err", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # -P: a stray json.py in whatever folder this was run from must not load beside a credential.
        return subprocess.Popen([sys.executable, "-P", "-m", HOST_MODULE, "--spec", str(path)],
                                cwd=state.app_dir(spec.key), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=error_fd,
                                start_new_session=True, close_fds=True)
    finally:
        os.close(error_fd)


def _wait_for_host(key: str, proc: subprocess.Popen, run_id: str) -> bool:
    deadline = time.monotonic() + HOST_START_WAIT_SECONDS
    while time.monotonic() < deadline:
        if state.read_json(state.run_file(key)).get("run_id") == run_id:
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.05)
    return False


def _cmd_start(args) -> int:
    key = args.key
    if state.host_alive(key):
        raise Refusal(f"{key} is already running")
    plan, srv = _plan(key, args)
    _refuse_wrong_platform(plan)
    failed = [item for item in _run_checks(plan) if not item["ok"]]
    if failed:
        print(f"{key} isn't ready to start:")
        _print_failed(failed)
        return 1
    _confirm_notice(plan, args.accept_notice)
    if plan.needs_acceptance:
        print("A newer version needs you to accept what changed; running the one you accepted.")
        for change in plan.changes:
            print(f"  {change}")
    if srv is None:
        run_id = f"dev-{uuid.uuid4()}"
    else:
        run_id = srv.start_run(key, plan.version)
    spec = RunSpec(key=key, version=plan.version, run_id=run_id,
                   max_minutes=plan.manifest["max_minutes"], canopy=list(plan.manifest["canopy"]),
                   sensitive=_is_sensitive(plan), unpublished=plan.unpublished, app=_child_spec(plan),
                   source=_child_spec(plan.source) if plan.source else None,
                   config=str(Path(args.config).expanduser().resolve()))
    proc = spawn_host(spec)
    if not _wait_for_host(key, proc, run_id):
        if srv is not None:
            try:
                srv.finish(run_id, "host_failed_to_start", {})
            except Exception:  # noqa: BLE001 — the server notices a run with no heartbeats anyway
                pass
        raise Refusal(f"{key} didn't start; see {state.app_dir(key) / 'host.err'}")
    label = " (unpublished)" if plan.unpublished else ""
    print(f"{key} {plan.version}{label} started. Stop it with: canopy-runner app stop {key}")
    return 0


# --- reading -----------------------------------------------------------------------


def _list_entry_from_dev(key: str, record: dict) -> dict:
    try:
        manifest = package.read_manifest(Path(record["path"]))
    except package.PackageError as exc:
        return {"key": key, "name": key, "version": "", "unpublished": True, "refusal": str(exc),
                "notice": "", "disclosure": {}, "provides": [], "consumes": [], "checks": []}
    return {"key": key, "name": manifest["name"], "version": manifest["version"], "unpublished": True,
            "notice": manifest["notice"], "disclosure": manifest["disclosure"],
            "provides": manifest["provides"], "consumes": manifest["consumes"],
            "checks": manifest["checks"], "entry": manifest["entry"],
            "max_minutes": manifest["max_minutes"], "settings": record.get("settings") or {},
            "needs_acceptance": False, "changes": [], "refusal": None, "source_key": None}


def _cmd_list(args) -> int:
    offline = ""
    apps: list = []
    try:
        apps = _server(args).list_apps()
    except (server.Unreachable, Refusal) as exc:
        offline = str(exc)
    dev = _dev_entries()
    apps = [a for a in apps if a.get("key") not in dev]
    apps += [_list_entry_from_dev(key, record) for key, record in sorted(dev.items())]
    for app in apps:
        app.setdefault("unpublished", False)
        try:
            local = state.describe(str(app.get("key")))
        except ValueError:
            continue
        # What is running here is this computer's own knowledge, fresher than the server's.
        app.update(running=local["running"], state=local["state"], stop_reason=local["stop_reason"],
                   session_url=local["session_url"], counts=local["counts"])
    if args.json:
        print(json.dumps({"apps": apps, "offline": bool(offline), "error": offline}))
        return 0
    if offline:
        print(f"Couldn't reach canopy, so this list shows only what's on this computer: {offline}")
    if not apps:
        print("No apps installed. Browse them in the app store.")
    for app in apps:
        tags = [t for t, on in (("running", app.get("running")), ("unpublished", app.get("unpublished"))) if on]
        suffix = f"  [{', '.join(tags)}]" if tags else ""
        print(f"{app.get('key')}  {app.get('version')}{suffix}")
        if app.get("refusal"):
            print(f"    can't start: {app['refusal']}")
    return 0


def _cmd_check(args) -> int:
    plan, _srv = _plan(args.key, args)
    _refuse_wrong_platform(plan)
    results = _run_checks(plan)
    ok = all(item["ok"] for item in results)
    if args.json:
        print(json.dumps({"key": args.key, "ok": ok, "refusal": None, "checks": results}))
        return 0 if ok else 1
    for item in results:
        print(f"  {'ok' if item['ok'] else 'NOT READY'}: {item['label']}")
        if not item["ok"]:
            print(f"    to fix: {item['fix']}")
    if not results:
        print(f"{args.key} has no checks.")
    return 0 if ok else 1


def _status_rows(key: Optional[str]) -> list:
    keys = [key] if key else state.installed_keys()
    return [state.describe(k) for k in keys]


def _cmd_status(args) -> int:
    rows = _status_rows(args.key)
    if args.json:
        print(json.dumps({"apps": rows, "running": sum(1 for r in rows if r["running"])}))
        return 0
    if not rows:
        print("No apps on this computer.")
    for row in rows:
        version = f" {row['version']}" if row["version"] else ""
        unpublished = " (unpublished)" if row["unpublished"] else ""
        if row["running"]:
            print(f"{row['key']}{version}{unpublished}: running, {row['state']}")
        elif row["stop_reason"]:
            print(f"{row['key']}{version}{unpublished}: stopped ({row['stop_reason']})")
        else:
            print(f"{row['key']}{version}{unpublished}: not running")
        if row["session_url"]:
            print(f"  session: {row['session_url']}")
        if row["counts"]:
            print(f"  {', '.join(f'{k}={v}' for k, v in sorted(row['counts'].items()))}")
    return 0


# --- stopping ----------------------------------------------------------------------


def _stopped(key: str) -> bool:
    return not state.host_alive(key) or bool(state.read_json(state.run_file(key)).get("stopped_at"))


def _force_kill(key: str) -> None:
    record = state.read_json(state.run_file(key))
    for pid in record.get("children") or []:
        try:
            os.killpg(int(pid), signal.SIGKILL)
        except (OSError, ValueError, TypeError):
            pass
    try:
        os.kill(int(record.get("pid")), signal.SIGKILL)
    except (OSError, ValueError, TypeError):
        pass
    record.update(stopped_at=time.time(), stop_reason=KILLED_BY_STOP, state="stopped")
    state.write_json(state.run_file(key), record)


def stop_apps(keys: list, *, wait: float = STOP_WAIT_SECONDS) -> dict:
    """Signal every supervisor first and then wait, so a slow one can't delay the others."""
    outcome = {}
    waiting = []
    for key in keys:
        if not state.host_alive(key):
            outcome[key] = "not running"
            continue
        try:
            os.kill(int(state.read_json(state.run_file(key)).get("pid")), signal.SIGTERM)
        except (OSError, ValueError, TypeError):
            pass
        waiting.append(key)
    deadline = time.monotonic() + wait
    while waiting and time.monotonic() < deadline:
        waiting_now = []
        for key in waiting:
            if _stopped(key):
                outcome[key] = "stopped"
            else:
                waiting_now.append(key)
        waiting = waiting_now
        if waiting:
            time.sleep(0.1)
    for key in waiting:
        _force_kill(key)
        outcome[key] = "killed"
    return outcome


def _cmd_stop(args) -> int:
    if args.all == bool(args.key):
        print("usage: canopy-runner app stop <key> | --all")
        return 1
    keys = state.installed_keys() if args.all else [args.key]
    outcome = stop_apps(keys)
    acted = {k: v for k, v in outcome.items() if v != "not running" or not args.all}
    if not acted:
        print("Nothing was running.")
    for key, what in acted.items():
        print(f"{key}: {what}")
    return 0


# --- building ----------------------------------------------------------------------


def _cmd_pack(args) -> int:
    manifest, packed = package.pack(Path(args.folder))
    out = Path(args.out) if args.out else Path(f"{manifest['key']}-{manifest['version']}.zip")
    out.write_bytes(packed)
    print(f"{out}\nsha256 {package.sha256(packed)}\n{len(packed)} bytes")
    return 0


def _base_url(args) -> str:
    from ..pair import DEFAULT_BASE_URL

    url = state.read_json(Path(args.config)).get("base_url")
    return str(url) if url else DEFAULT_BASE_URL


def _cmd_submit(args) -> int:
    token = server.person_token()
    manifest, packed = package.pack(Path(args.folder))
    answer = server.submit(_base_url(args), token, packed, args.note or "")
    print(f"{manifest['key']} {manifest['version']}: {answer.get('status')}")
    if answer.get("review_url"):
        print(f"review: {answer['review_url']}")
    print(f"sha256 {answer.get('sha256') or package.sha256(packed)}")
    return 0


def _parse_settings(pairs: list) -> dict:
    out = {}
    for pair in pairs or []:
        name, separator, value = pair.partition("=")
        if not separator or not name:
            raise Refusal(f"--set wants name=value, not {pair!r}")
        out[name] = {"true": True, "false": False}.get(value, value)
    return out


def _cmd_dev_install(args) -> int:
    folder = Path(args.folder).expanduser().resolve()
    manifest = package.read_manifest(folder)
    key = manifest["key"]
    if state.host_alive(key):
        raise Refusal(f"{key} is running; stop it first")
    known = set(manifest["settings"])
    settings = _parse_settings(args.set)
    unknown = set(settings) - known
    if unknown:
        raise Refusal(f"{key} has no setting {', '.join(sorted(unknown))}")
    state.ensure_dir(key)
    state.write_json(state.dev_file(key), {"path": str(folder), "installed_at": time.time(),
                                           "settings": settings})
    print(f"{key} {manifest['version']} runs from {folder} (unpublished). "
          f"Its Canopy calls are written to {state.would_send_file(key)}; nothing is sent.")
    return 0


def _cmd_remove(args) -> int:
    key = args.key
    if state.host_alive(key):
        raise Refusal(f"{key} is running; stop it first")
    was_dev = state.dev_file(key).exists()
    package.remove_tree(state.app_dir(key))
    if was_dev:
        print(f"{key}: removed from this computer. Its folder is untouched.")
    else:
        print(f"{key}: local files removed. Remove the installation itself from the app store.")
    return 0


COMMANDS = {
    "list": _cmd_list, "check": _cmd_check, "start": _cmd_start, "status": _cmd_status,
    "stop": _cmd_stop, "pack": _cmd_pack, "submit": _cmd_submit,
    "dev-install": _cmd_dev_install, "remove": _cmd_remove,
}


def run(args) -> int:
    try:
        return COMMANDS[args.app_command](args)
    except (Refusal, server.Refused, server.Unreachable, package.PackageError, ValueError) as exc:
        if getattr(args, "json", False) and args.app_command == "check":
            print(json.dumps({"key": args.key, "ok": False, "refusal": str(exc), "checks": []}))
        else:
            print(str(exc))
        return 1
