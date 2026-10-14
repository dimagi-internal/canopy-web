"""``canopy-runtime publish-capabilities`` — send an agent's declared capabilities to canopy-web.

Part of the agent's deploy, not a hand step:

    canopy-runtime publish-capabilities --spec runtime.yaml --agent ace

loads every ``capabilities[].manifest`` (paths relative to the spec file),
validates each with ``CapabilityManifest``, and sends the whole list in one
``PUT /api/agents/<slug>/capabilities``. canopy-web validates them again,
resolves each stack's Outputs, and retires any capability the list leaves out
(releasing its live leases). Idempotent: publishing the same list twice changes
nothing.

Stdlib HTTP on purpose — this package is Django-free and installs on a bare box.

Exit codes: 0 published · 1 canopy-web refused or could not be reached ·
2 the spec or a manifest is invalid.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from canopy_runtime.schema import CapabilityManifest, load_capability_manifest, load_runtime_yaml

DEFAULT_BASE_URL = "https://canopy.dimagi.com"


def load_manifests(spec_path: Path) -> list[CapabilityManifest]:
    """Every manifest the spec declares, validated, in declaration order.
    Raises ValueError naming the file at fault."""
    spec = load_runtime_yaml(spec_path.read_text())
    root = spec_path.resolve().parent
    manifests: list[CapabilityManifest] = []
    seen: dict[str, str] = {}
    for ref in spec.capabilities:
        path = (root / ref.manifest).resolve()
        try:
            manifest = load_capability_manifest(path.read_text())
        except OSError as exc:
            raise ValueError(f"{ref.manifest}: cannot read ({exc})") from exc
        except ValueError as exc:
            raise ValueError(f"{ref.manifest}: {exc}") from exc
        if manifest.name in seen:
            raise ValueError(f"{ref.manifest}: capability {manifest.name!r} is already declared "
                             f"by {seen[manifest.name]}")
        seen[manifest.name] = ref.manifest
        manifests.append(manifest)
    return manifests


def payload(manifests: list[CapabilityManifest]) -> dict:
    return {"capabilities": [m.model_dump(mode="json") for m in manifests]}


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
    ap.add_argument("--spec", required=True, help="path to the agent's runtime.yaml")
    ap.add_argument("--agent", required=True, help="the agent's canopy slug")
    ap.add_argument("--base-url", default=os.environ.get("CANOPY_BASE_URL") or DEFAULT_BASE_URL)
    ap.add_argument("--token", default=os.environ.get("CANOPY_TOKEN") or os.environ.get("CANOPY_WEB_PAT"),
                    help="canopy-web PAT (default: $CANOPY_TOKEN, then $CANOPY_WEB_PAT)")
    ap.add_argument("--dry-run", action="store_true", help="validate and print the body; send nothing")
    args = ap.parse_args(argv)

    try:
        manifests = load_manifests(Path(args.spec))
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
        print(f"published {args.agent}/{cap.get('name')} ({cap.get('kind')}, stack {cap.get('stack')}): {grant}")
    for name in resp.get("retired", []):
        print(f"retired {args.agent}/{name}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
