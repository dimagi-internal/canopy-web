#!/usr/bin/env python3
"""Per-platform build records and the release manifest.json built from them.

    manifest.py record   --platform windows-x86_64 --sha <sha> --installer X.exe --update X.exe \
                         [--signed] [--notarized] --out windows-x86_64.json
    manifest.py assemble --sha <sha> --committed-at <epoch> --built-at <iso> --records DIR --out manifest.json \
                         [--allow-partial]

A record is what one build job knows: its files' sha256 + size and whether it was
ACTUALLY signed / notarized (macos/build_app.sh writes the same shape itself).
`assemble` is the publish job's: it refuses records for another sha or a missing
platform, so a manifest can never describe a half-built release — unless the publish
job says --allow-partial because a platform's build or smoke test failed: then the
manifest names only the platforms that passed (at least one), and canopy-web serves
nothing for the others at that sha (`runner_releases.artifact` answers None). The schema is the
contract in docs/superpowers/specs/2026-10-19-runner-installers-design.md § Releases.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PLATFORMS = ("macos-arm64", "macos-x86_64", "windows-x86_64")


def file_entry(path: Path) -> dict:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return {"file": path.name, "sha256": h.hexdigest(), "size": path.stat().st_size}


def record(args) -> int:
    rec = {"platform": args.platform, "sha": args.sha,
           "installer": file_entry(Path(args.installer)), "update": file_entry(Path(args.update)),
           "signed": args.signed, "notarized": args.notarized}
    Path(args.out).write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(rec))
    return 0


def assemble(args) -> int:
    artifacts = {}
    for path in sorted(Path(args.records).rglob("*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        if rec.get("platform") not in PLATFORMS:
            continue
        if rec.get("sha") != args.sha:
            raise SystemExit(f"{path}: record is for {rec.get('sha')}, not {args.sha}")
        if rec["platform"] in artifacts:
            raise SystemExit(f"{path}: a second record for {rec['platform']}")
        artifacts[rec["platform"]] = {
            "installer": rec["installer"], "update": rec["update"],
            "signed": bool(rec["signed"]), "notarized": bool(rec["notarized"]),
        }
    missing = [p for p in PLATFORMS if p not in artifacts]
    if missing and not (args.allow_partial and artifacts):
        raise SystemExit(f"no build record for {', '.join(missing)} — refusing a partial manifest")
    if missing:
        print(f"partial manifest: no {', '.join(missing)}", file=sys.stderr)
    manifest = {"sha": args.sha, "committed_at": int(args.committed_at), "built_at": args.built_at,
                "artifacts": {p: artifacts[p] for p in PLATFORMS if p in artifacts}}
    Path(args.out).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--platform", required=True, choices=PLATFORMS)
    r.add_argument("--sha", required=True)
    r.add_argument("--installer", required=True)
    r.add_argument("--update", required=True)
    r.add_argument("--signed", action="store_true")
    r.add_argument("--notarized", action="store_true")
    r.add_argument("--out", required=True)
    a = sub.add_parser("assemble")
    a.add_argument("--sha", required=True)
    a.add_argument("--committed-at", required=True)
    a.add_argument("--built-at", required=True)
    a.add_argument("--records", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--allow-partial", action="store_true",
                   help="publish the platforms that have a record (at least one) instead of refusing")
    args = ap.parse_args()
    return record(args) if args.cmd == "record" else assemble(args)


if __name__ == "__main__":
    sys.exit(main())
