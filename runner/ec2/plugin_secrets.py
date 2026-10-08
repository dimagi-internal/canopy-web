#!/usr/bin/env python3
"""Flatten a required plugin's `config/secrets.yaml` into rows bootstrap can stage.

    name <TAB> item/field <TAB> repo-relative target <TAB> mode

A plugin cloned as a DEPENDENCY (`required_plugins`, e.g. chrome-sales) runs from
that clone, and its MCP servers read their keys from files in it — chrome-sales'
gdrive server reads `<plugin-root>/.gws-sa-key.json`. On a laptop a human runs
`canopy provision` once; on the cloud box nothing did, so the server started with
no key (canopy-web#1237).

Only REQUIRED secrets whose target is inside the plugin (`{repo}/…`) are emitted:
  - `optional: true` is skipped. chrome-sales' Salesforce creds are optional, and
    on the box they are deliberately NOT a plugin-root file: an agent turn acts in
    Salesforce only as the identity its agent borrows (canopy-web#1291), staged
    per agent by the runner. A box-wide file would be a second, unowned identity.
  - a target outside the plugin (`~/…`) is not this clone's to write.

The vault in the declared `op://<vault>/<item>/<field>` is dropped: it names the
vault a laptop operator reads, and the box has exactly two it may read — the
agent's own and the workspace's shared one — each with its own key. Bootstrap
looks the same item up in those, in that order.

Silent on anything malformed or unreadable (including no PyYAML): a dependency's
secrets list must never take the bootstrap down.
"""
from __future__ import annotations

import sys

REPO_PREFIX = "{repo}/"


def rows(data: object) -> list[tuple[str, str, str, str]]:
    out: list[tuple[str, str, str, str]] = []
    entries = data.get("secrets") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("optional") is True:
            continue
        name = str(entry.get("name") or "").strip()
        ref = str(entry.get("op") or "").strip()
        target = str(entry.get("target") or "").strip()
        mode = str(entry.get("mode") or "0600").strip()
        if not (name and ref.startswith("op://") and target.startswith(REPO_PREFIX)):
            continue
        parts = ref[len("op://"):].split("/", 1)
        if len(parts) != 2 or not parts[1]:
            continue
        rel = target[len(REPO_PREFIX):]
        # A target that climbs out of the clone is not the clone's to write.
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            continue
        fields = (name, parts[1], rel, mode)
        if any("\t" in f or "\n" in f for f in fields):
            continue
        out.append(fields)
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    try:
        import yaml  # cloud-init depends on PyYAML, so the box's python3 has it
        with open(argv[1]) as fh:
            data = yaml.safe_load(fh)
    except Exception:
        return 0
    for row in rows(data):
        print("\t".join(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
