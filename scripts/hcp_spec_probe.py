#!/usr/bin/env python3
"""Has the HCP spec moved? Two questions, answered from the spec site, never failing a build.

canopy implements HCP v1 draft 4 (`docs/architecture/hcp-conformance.md`). Two things
outside this repository decide what it can claim next:

1. **The JSON Schema** (spec 2.1.1, the one open Core MUST). Appendix A says it *will be*
   published at https://hcp.me/schemas/v1. As of the last look that URL answers 200
   `text/html`, the site's single-page-app shell, so there is nothing to validate
   against. This probe notices when the URL starts returning JSON (a JSON content
   type, or a body that parses to an object with a `$schema`).
2. **A newer draft.** hcp.me is a single-page app: its pages are rendered by script, so
   the probe reads the HTML and the script bundles it links for `draft-N` / `draft N`
   and compares N with the draft this repository implements. (As of the last look the
   site states no draft at all, so "nothing found" is the expected answer and is not
   news.)

It prints a JSON report and exits 0 whatever it finds: an unreachable site or a changed
page must never fail unrelated work. `--github-output` / `--body-file` hand the scheduled
workflow (`.github/workflows/hcp-spec-probe.yml`) what it needs to open an issue.

    python scripts/hcp_spec_probe.py [--github-output "$GITHUB_OUTPUT"] [--body-file body.md]

Stdlib only; `fetch` is injectable so `tests/test_hcp_spec_probe.py` runs it offline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request
from typing import Callable

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_URL = "https://hcp.me/schemas/v1"
SITE_URL = "https://hcp.me/"
CONFORMANCE_DOC = ROOT / "docs" / "architecture" / "hcp-conformance.md"
MAX_BUNDLES = 4
MAX_BYTES = 4_000_000
JSON_TYPES = ("application/json", "application/schema+json", "application/ld+json")

#: What closes conformance 2.1.1, said the same way in the issue and in the docs.
WHAT_CLOSES_2_1_1 = (
    "What closes conformance 2.1.1 (the one open Core MUST in "
    "`docs/architecture/hcp-conformance.md`): validate Tier 1 entries against this schema on "
    "write and when emitting them (`apps/contacts/hcp/entries.py`), drop the entry from "
    "`hcp.KNOWN_GAPS` (`apps/contacts/hcp/conformance.py`), update the conformance table, and "
    "only then consider `hcp.CONFORMANCE_LEVEL = \"HCP-v1-Interop\"` (a decision for the owner: "
    "Interop also needs `authorization_profile: oauth2`, which discovery already declares)."
)

Fetch = Callable[[str], "tuple[int | None, str, bytes]"]


def fetch(url: str) -> tuple[int | None, str, bytes]:
    """(status, content-type, first MAX_BYTES of the body); status None on any network failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "canopy-web-hcp-spec-probe/1",
                                               "Accept": "application/schema+json, application/json, text/html"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — fixed https URLs above
            return resp.status, resp.headers.get("Content-Type", ""), resp.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "") if e.headers else "", b""
    except Exception as e:  # noqa: BLE001 — a probe never raises
        return None, f"{type(e).__name__}: {e}"[:200], b""


def implemented_draft(doc: pathlib.Path = CONFORMANCE_DOC) -> int:
    """The draft number this repository says it implements (`1.0-draft-N` in the audit)."""
    m = re.search(r"1\.0-draft-(\d+)", doc.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"{doc} no longer names the draft it audits (1.0-draft-N)")
    return int(m.group(1))


def classify_schema(status: int | None, content_type: str, body: bytes) -> dict:
    """state: published | json-without-schema | blocked | unreachable. `news` is whether
    a person should look."""
    if status is None:
        return {"state": "unreachable", "news": False, "detail": content_type}
    ctype = (content_type or "").split(";")[0].strip().lower()
    parsed = None
    try:
        parsed = json.loads(body.decode("utf-8")) if body.strip()[:1] in (b"{", b"[") else None
    except (ValueError, UnicodeDecodeError):
        parsed = None
    if status == 200 and isinstance(parsed, dict) and "$schema" in parsed:
        return {"state": "published", "news": True,
                "detail": f"200 {ctype or 'unknown type'}, a JSON object with $schema={parsed['$schema']!r}"}
    if status == 200 and (ctype in JSON_TYPES or ctype.endswith("+json")):
        return {"state": "json-without-schema", "news": True,
                "detail": f"200 {ctype}, but no $schema key: read it before relying on it"}
    return {"state": "blocked", "news": False,
            "detail": f"{status} {ctype or 'no content type'} (not a schema)"}


_DRAFT = re.compile(r"draft[-\s_]?(\d{1,2})\b", re.I)


def drafts_in(text: str) -> set[int]:
    return {int(n) for n in _DRAFT.findall(text)}


def bundle_urls(html: str, base: str = SITE_URL) -> list[str]:
    urls = []
    for src in re.findall(r"""<script[^>]+src=["']([^"']+\.js)["']""", html):
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = base.rstrip("/") + src
        if src.startswith(base) and src not in urls:
            urls.append(src)
    return urls[:MAX_BUNDLES]


def check_site(get: Fetch, implemented: int) -> dict:
    status, ctype, body = get(SITE_URL)
    if status != 200:
        return {"state": "unreachable", "news": False, "detail": f"{status} {ctype}"[:200], "drafts": []}
    html = body.decode("utf-8", "ignore")
    found = drafts_in(html)
    fingerprint = hashlib.sha256(body).hexdigest()[:12]
    scanned = 0
    for url in bundle_urls(html):
        s, _, js = get(url)
        if s == 200:
            scanned += 1
            found |= drafts_in(js.decode("utf-8", "ignore"))
    newest = max(found) if found else None
    if newest is not None and newest > implemented:
        return {"state": "newer-draft", "news": True, "drafts": sorted(found),
                "detail": f"the site mentions draft {newest}; this repository implements draft {implemented}"}
    return {"state": "no-newer-draft", "news": False, "drafts": sorted(found),
            "detail": (f"drafts named on the site: {sorted(found) or 'none (the site states no draft)'}; "
                       f"implemented: {implemented}; scanned the page and {scanned} script bundle(s)"),
            "page_fingerprint": fingerprint}


def probe(get: Fetch = fetch, implemented: int | None = None) -> dict:
    implemented = implemented if implemented is not None else implemented_draft()
    schema = classify_schema(*get(SCHEMA_URL))
    site = check_site(get, implemented)
    return {"schema_url": SCHEMA_URL, "implemented_draft": implemented, "schema": schema, "site": site,
            "news": schema["news"] or site["news"]}


def body_for(report: dict) -> str:
    lines = ["The scheduled HCP spec probe (`scripts/hcp_spec_probe.py`) found something to look at.", ""]
    schema, site = report["schema"], report["site"]
    if schema["news"]:
        lines += [f"**The JSON Schema moved** — {SCHEMA_URL}: {schema['detail']}.", "", WHAT_CLOSES_2_1_1, ""]
    if site["news"]:
        lines += [f"**A newer draft** — {site['detail']}. Read what changed, then re-audit "
                  "`docs/architecture/hcp-conformance.md` against it (it names the draft it audits).", ""]
    lines += ["```json", json.dumps(report, indent=2), "```"]
    return "\n".join(lines)


def title_for(report: dict) -> str:
    parts = []
    if report["schema"]["news"]:
        parts.append("the JSON Schema is published (closes conformance 2.1.1)")
    if report["site"]["news"]:
        parts.append("a newer draft is out")
    return "HCP spec: " + " and ".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--github-output", help="append news=true|false and title=... for a workflow")
    ap.add_argument("--body-file", help="write the issue body here when there is news")
    args = ap.parse_args(argv)
    report = probe()
    print(json.dumps(report, indent=2))
    if report["news"] and args.body_file:
        pathlib.Path(args.body_file).write_text(body_for(report), encoding="utf-8")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"news={'true' if report['news'] else 'false'}\n")
            if report["news"]:
                fh.write(f"title={title_for(report)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
