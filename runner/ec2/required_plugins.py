#!/usr/bin/env python3
"""Flatten an agent's `required_plugins` into tab-separated rows for bootstrap.

    name <TAB> marketplace <TAB> marketplace_name <TAB> note

Its own file rather than a heredoc inside bootstrap_agents.sh so it can be tested
directly, and so the parsing rules live somewhere a reader can find them.

The contract is the one `canopy agent doctor`'s `check_required_plugins` already
reads, and the two MUST agree: the doctor failing an agent for a missing plugin
that bootstrap could not have installed (or vice versa) is worse than either check
alone. An entry is a bare name, or an object with `marketplace` ("owner/repo"),
optional `marketplace_name` (defaults to the plugin name — true across this fleet:
eva@eva, chrome-sales@chrome-sales) and an optional `note` for the follow-up a
human still owes.

Silent on anything malformed: a broken or absent config must not take the whole
bootstrap down over an optional dependency list.
"""
from __future__ import annotations

import json
import sys


def rows(data: dict) -> list[tuple[str, str, str, str]]:
    out: list[tuple[str, str, str, str]] = []
    entries = data.get("required_plugins") or []
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if isinstance(entry, str):
            name = entry.strip()
            if name:
                out.append((name, "", name, ""))
            continue
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        market = str(entry.get("marketplace") or "").strip()
        mname = str(entry.get("marketplace_name") or "").strip() or name
        note = " ".join(str(entry.get("note") or "").split())  # never break the TSV
        out.append((name, market, mname, note))
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    try:
        with open(argv[1]) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    for row in rows(data):
        print("\t".join(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
