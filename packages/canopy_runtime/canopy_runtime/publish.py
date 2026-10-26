"""``canopy-runtime publish-capabilities`` — send an agent's capabilities to canopy-web.

Part of the agent's deploy, not a hand step:

    canopy-runtime publish-capabilities --dir infra/capabilities --agent ace
    canopy-runtime publish-capabilities --manifest a/capability.yaml --manifest b/capability.yaml --agent ace

``--dir`` takes every ``<dir>/*/capability.yaml``; ``--manifest`` names one file and
repeats. Each is validated with ``CapabilityManifest`` and the whole list goes in one
``PUT /api/agents/<slug>/capabilities``. canopy-web validates them again, resolves
each stack's Outputs, and RETIRES ANY CAPABILITY THE LIST LEAVES OUT (releasing its
live leases), so the list is the agent's complete set: ``--dir`` is the form to use,
and it refuses an empty directory rather than retire everything. Idempotent:
publishing the same list twice changes nothing.

Stdlib HTTP on purpose — this package is Django-free and installs on a bare box.

Exit codes: 0 published · 1 canopy-web refused or could not be reached ·
2 the arguments or a manifest are invalid.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from canopy_runtime.schema import CapabilityManifest, load_capability_manifest

DEFAULT_BASE_URL = "https://canopy.dimagi.com"
USAGE = ("canopy-runtime publish-capabilities (--dir infra/capabilities | --manifest PATH ...) "
         "--agent <slug> [--base-url URL] [--token PAT] [--dry-run]")


def find_manifests(directory: Path) -> list[Path]:
    """Every ``<directory>/*/capability.yaml``, sorted. Raises ValueError if the
    directory is missing, holds none (publishing an empty list retires every
    capability), or has a subdirectory whose manifest is misnamed (a
    ``capability.yml`` the glob would silently skip, retiring that capability)."""
    if not directory.is_dir():
        raise ValueError(f"{directory}: not a directory")
    found = sorted(directory.glob("*/capability.yaml"))
    for sub in sorted(p for p in directory.iterdir() if p.is_dir()):
        for odd in ("capability.yml", "capability.yaml.disabled"):
            if (sub / odd).exists():
                raise ValueError(f"{sub / odd}: manifests are named capability.yaml")
    if not found:
        raise ValueError(f"{directory}: no */capability.yaml found; publishing nothing would "
                         "retire every capability this agent has")
    return found


def load_manifests(paths: list[Path]) -> list[CapabilityManifest]:
    """Every manifest, validated, in the order given. Raises ValueError naming the
    file at fault (unreadable, invalid, or a repeat of an earlier capability name)."""
    manifests: list[CapabilityManifest] = []
    seen: dict[str, Path] = {}
    for path in paths:
        try:
            manifest = load_capability_manifest(path.read_text())
        except OSError as exc:
            raise ValueError(f"{path}: cannot read ({exc})") from exc
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
        if manifest.name in seen:
            raise ValueError(f"{path}: capability {manifest.name!r} is already declared "
                             f"by {seen[manifest.name]}")
        seen[manifest.name] = path
        manifests.append(manifest)
    return manifests


def payload(manifests: list[CapabilityManifest]) -> dict:
    # exclude_none: an unset optional (an ec2-ephemeral manifest's `slots`, a
    # pool's `aws`/`exec`, a param's `default`/`enum`) is left out rather than
    # sent as null, so an ec2-ephemeral body stays exactly what a canopy-web
    # from before `pool` existed accepts (its manifest model forbids unknown keys).
    return {"capabilities": [m.model_dump(mode="json", exclude_none=True) for m in manifests]}


def put(base_url: str, agent: str, token: str, body: dict, *, timeout: float = 60.0) -> tuple[int, dict]:
    url = f"{base_url.rstrip('/')}/api/agents/{agent}/capabilities"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="PUT",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": f"Bearer {token}", "X-Canopy-Client": "canopy-runtime"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — operator-given URL
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except ValueError:
            return exc.code, {"detail": raw.decode(errors="replace")[:500]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="canopy-runtime publish-capabilities")
    ap.add_argument("--manifest", action="append", default=[], metavar="PATH",
                    help="a capability.yaml to publish (repeatable)")
    ap.add_argument("--dir", action="append", default=[], metavar="DIR",
                    help="publish every DIR/*/capability.yaml (repeatable)")
    ap.add_argument("--spec", help=argparse.SUPPRESS)  # retired: fails loudly below
    ap.add_argument("--agent", required=True, help="the agent's canopy slug")
    ap.add_argument("--base-url", default=os.environ.get("CANOPY_BASE_URL") or DEFAULT_BASE_URL)
    ap.add_argument("--token", default=os.environ.get("CANOPY_TOKEN") or os.environ.get("CANOPY_WEB_PAT"),
                    help="canopy-web PAT (default: $CANOPY_TOKEN, then $CANOPY_WEB_PAT)")
    ap.add_argument("--dry-run", action="store_true", help="validate and print the body; send nothing")
    args = ap.parse_args(argv)

    if args.spec:
        print(f"error: --spec is retired: capabilities publish from their "
              f"capability.yaml manifests, not from a spec file. Publish from the capability manifests directly:\n  {USAGE}", file=sys.stderr)
        return 2
    if not (args.manifest or args.dir):
        print(f"error: name the manifests to publish:\n  {USAGE}", file=sys.stderr)
        return 2
    try:
        paths: list[Path] = []
        for d in args.dir:
            paths += find_manifests(Path(d))
        paths += [Path(m) for m in args.manifest]
        once: dict[Path, Path] = {}  # the same file via --dir and --manifest is one capability
        for p in paths:
            once.setdefault(p.resolve(), p)
        paths = list(once.values())
        manifests = load_manifests(paths)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    body = payload(manifests)
    if args.dry_run:
        print(json.dumps(body, indent=2))
        return 0
    if not args.token:
        print("error: no token: pass --token or set CANOPY_TOKEN", file=sys.stderr)
        return 2
    try:
        status, resp = put(args.base_url, args.agent, args.token, body)
    except (urllib.error.URLError, OSError) as exc:
        print(f"error: could not reach {args.base_url}: {exc}", file=sys.stderr)
        return 1
    if status != 200:
        detail = resp.get("detail") or resp.get("title") or resp
        print(f"error: canopy-web refused the capabilities ({status}): {detail}", file=sys.stderr)
        return 1
    for cap in resp.get("capabilities", []):
        grant = "granted" if cap.get("grant") else "NOT granted (ask a workspace admin)"
        where = (f"slots {', '.join((cap.get('manifest') or {}).get('slots') or [])}" if cap.get("kind") == "pool"
                 else f"stack {cap.get('stack')}")
        print(f"published {args.agent}/{cap.get('name')} ({cap.get('kind')}, {where}): {grant}")
    for name in resp.get("retired", []):
        print(f"retired {args.agent}/{name}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
