#!/usr/bin/env python3
"""Build the first-boot seed of cloud_runner.py for Secrets Manager.

A fresh box has nothing to run until `canopy-fetch-env` (runner.cfn.yaml)
installs cloud_runner.py from Secrets Manager. That used to be ONE secret holding
the file as gzip+base64, which stopped deploying once the runner grew: Secrets
Manager caps a value at 65536 bytes, and on 2026-10-04 the encoded file was
91 KB, so every `up.sh` died on PutSecretValue with a ValidationException. xz or
bz2 would have bought a few weeks (79 KB / 73 KB — still over).

So the seed is now a MANIFEST plus N parts, which scales with the file:

    canopy/cloud-runner/runner-seed          {"format": ..., "parts": N,
                                              "sha256": <of the .py>,
                                              "sha": <git sha>, "committed_at": ...}
    canopy/cloud-runner/runner-seed-part-0   <first PART_SIZE chars of the gz+b64>
    canopy/cloud-runner/runner-seed-part-1   ...

The box concatenates the parts, decodes, and refuses to install bytes whose
sha256 does not match the manifest — so a box that boots WHILE up.sh is mid-way
through a publish fails its ExecStartPre and retries (Restart=on-failure),
rather than running half of one version and half of another. Parts are written
before the manifest for the same reason.

Provenance (`sha`, `committed_at`) lives in the manifest, replacing the separate
runner-code-sha secret: one read tells the box both what it is installing and
what that is.

Stdlib only; runs on the operator's machine (up.sh) and in tests.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import pathlib
import sys

FORMAT = "gzip+base64/parts-v1"
#: Comfortably under Secrets Manager's 65536-byte SecretString cap.
PART_SIZE = 60000


def encode(data: bytes) -> str:
    """gzip (mtime pinned, so the same file always encodes the same) + base64."""
    return base64.b64encode(gzip.compress(data, mtime=0)).decode("ascii")


def split(text: str, size: int = PART_SIZE) -> list[str]:
    if size <= 0:
        raise ValueError("part size must be positive")
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def build(data: bytes, *, sha: str = "", committed_at: int = 0,
          size: int = PART_SIZE) -> tuple[dict, list[str]]:
    parts = split(encode(data), size)
    manifest = {
        "format": FORMAT,
        "parts": len(parts),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "sha": sha,
        "committed_at": int(committed_at or 0),
        "ref": "seed",
    }
    return manifest, parts


def assemble(manifest: dict, parts: list[str]) -> bytes:
    """The inverse, for tests and for update_runner.sh's --from-secret parity.
    The box itself decodes in shell (canopy-fetch-env); a test pins the two."""
    if manifest.get("format") != FORMAT:
        raise ValueError(f"unknown seed format {manifest.get('format')!r}")
    if len(parts) != int(manifest["parts"]):
        raise ValueError(f"expected {manifest['parts']} parts, got {len(parts)}")
    data = gzip.decompress(base64.b64decode("".join(p.strip() for p in parts)))
    if hashlib.sha256(data).hexdigest() != manifest["sha256"]:
        raise ValueError("seed sha256 mismatch")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="write manifest.json + part-<i> into OUT_DIR")
    b.add_argument("source")
    b.add_argument("out_dir")
    b.add_argument("--sha", default="")
    b.add_argument("--committed-at", type=int, default=0)
    b.add_argument("--part-size", type=int, default=PART_SIZE)
    a = sub.add_parser("assemble", help="rebuild the source from OUT_DIR (verifies sha256)")
    a.add_argument("out_dir")
    a.add_argument("dest")
    args = ap.parse_args(argv)

    if args.cmd == "build":
        data = pathlib.Path(args.source).read_bytes()
        manifest, parts = build(data, sha=args.sha, committed_at=args.committed_at,
                                size=args.part_size)
        out = pathlib.Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for i, part in enumerate(parts):
            (out / f"part-{i}").write_text(part)
        (out / "manifest.json").write_text(json.dumps(manifest))
        print(len(parts))
        return 0

    out = pathlib.Path(args.out_dir)
    manifest = json.loads((out / "manifest.json").read_text())
    parts = [(out / f"part-{i}").read_text() for i in range(int(manifest["parts"]))]
    pathlib.Path(args.dest).write_bytes(assemble(manifest, parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
