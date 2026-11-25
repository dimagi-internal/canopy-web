#!/usr/bin/env python3
"""Has the HCP spec moved? Three questions, never failing a build.

canopy implements HCP v1 draft 4 on the wire and models v2 internally
(`docs/architecture/hcp-conformance.md`). Three things outside this repository decide
what it can claim next:

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

3. **The spec repository** (`loyalagents/hcp-spec`). The spec moved there: v2 drafts live
   in `spec/<version>-draft-<n>/`, and hcp.me (question 2) never showed them, which is how
   v2 went unnoticed. The probe lists `spec/` through the GitHub contents API and compares
   the newest `<major>.<minor>-draft-<n>` with the newest draft the conformance doc TRACKS
   for the same major version. Tracked means named on the doc's marker line
   (`<!-- hcp-spec-probe: tracked 1.0-draft-4, 2.0-draft-3 -->`), not merely mentioned
   somewhere in it: a draft quoted in passing must not quiet the probe. The repository is
   public today. The workflow passes `HCP_SPEC_TOKEN` (or, absent that, its own token) as
   `GH_SPEC_TOKEN`; if the repository goes private again and the token cannot read it, the
   answer is `no-access`, which IS news (the probe has gone blind and would otherwise stay
   quiet forever) and says what to add. The workflow runs weekly, so that is at most one
   issue comment a week.

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
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request
from typing import Callable

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_URL = "https://hcp.me/schemas/v1"
SITE_URL = "https://hcp.me/"
SPEC_REPO = "loyalagents/hcp-spec"
SPEC_DIR_URL = f"https://api.github.com/repos/{SPEC_REPO}/contents/spec"
TOKEN_ENV = "GH_SPEC_TOKEN"
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

Fetch = Callable[..., "tuple[int | None, str, bytes]"]


def fetch(url: str, headers: dict | None = None) -> tuple[int | None, str, bytes]:
    """(status, content-type, first MAX_BYTES of the body); status None on any network failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "canopy-web-hcp-spec-probe/1",
                                               "Accept": "application/schema+json, application/json, text/html",
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — fixed https URLs above
            return resp.status, resp.headers.get("Content-Type", ""), resp.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "") if e.headers else "", b""
    except Exception as e:  # noqa: BLE001 — a probe never raises
        return None, f"{type(e).__name__}: {e}"[:200], b""


def implemented_draft(doc: pathlib.Path = CONFORMANCE_DOC) -> int:
    """The v1 draft number this repository says it implements (`1.0-draft-N` in the audit)."""
    m = re.search(r"1\.0-draft-(\d+)", doc.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"{doc} no longer names the draft it audits (1.0-draft-N)")
    return int(m.group(1))


_VERSIONED = re.compile(r"\b(\d+)\.(\d+)-draft-(\d+)\b")


def versions_in(text: str) -> set[tuple[int, int, int]]:
    """Every `<major>.<minor>-draft-<n>` in the text, as (major, minor, n)."""
    return {tuple(int(x) for x in m) for m in _VERSIONED.findall(text)}


_TRACKED = re.compile(r"<!--\s*hcp-spec-probe:\s*tracked\s+(?P<versions>[^>]*?)\s*-->")


def tracked_versions(doc: pathlib.Path = CONFORMANCE_DOC) -> set[tuple[int, int, int]]:
    """The spec versions the conformance doc TRACKS: those on its marker line, and only those.
    A doc without the marker is an error (the probe would otherwise compare against nothing)."""
    m = _TRACKED.search(doc.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"{doc} has no tracked-version marker (<!-- hcp-spec-probe: tracked 1.0-draft-N, ... -->)")
    return versions_in(m.group("versions"))


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


def check_repo(get: Fetch, tracked: set[tuple[int, int, int]], token: str | None) -> dict:
    """Is there a spec draft in `loyalagents/hcp-spec` newer than any the doc names, for its major version?

    state: newer-draft | no-newer-draft | no-access | unreachable. `newer-draft` and `no-access` are
    news: a repository the probe cannot read would hide every later draft.
    """
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, ctype, body = get(SPEC_DIR_URL, headers)
    if status is None:
        return {"state": "unreachable", "news": False, "detail": ctype, "versions": []}
    if status in (401, 403, 404):
        # A private repo answers 404 to a caller that cannot see it. Said out loud: otherwise the
        # probe goes blind and nobody notices.
        why = "no token" if not token else "the token cannot read it"
        return {"state": "no-access", "news": True, "versions": [],
                "detail": f"{SPEC_REPO} answered {status} ({why}); add the {TOKEN_ENV} secret to watch it"}
    try:
        listing = json.loads(body.decode("utf-8"))
        names = [e["name"] for e in listing if isinstance(e, dict) and e.get("type") == "dir"]
    except (ValueError, UnicodeDecodeError, KeyError, TypeError):
        return {"state": "unreachable", "news": False, "versions": [],
                "detail": f"{status} {ctype}: not a directory listing"[:200]}
    found = sorted(versions_in("\n".join(names)))
    # "Newer" is per major version: a v1 draft 5 is news against v1 draft 4, and a v2 draft is
    # news until the doc names a v2 draft at least as new.
    news = []
    for v in found:
        have = [t for t in tracked if t[0] == v[0]]
        if not have or v > max(have):
            news.append(v)
    label = lambda v: f"{v[0]}.{v[1]}-draft-{v[2]}"  # noqa: E731
    if news:
        return {"state": "newer-draft", "news": True, "versions": [label(v) for v in found],
                "newer": [label(v) for v in news],
                "detail": (f"{SPEC_REPO} has {', '.join(label(v) for v in news)}; the conformance doc names "
                           f"{', '.join(label(v) for v in sorted(tracked)) or 'none'}")}
    return {"state": "no-newer-draft", "news": False, "versions": [label(v) for v in found],
            "detail": (f"{SPEC_REPO} drafts: {', '.join(label(v) for v in found) or 'none'}; "
                       "all already named in the doc")}


def probe(get: Fetch = fetch, implemented: int | None = None, token: str | None = None,
          tracked: set[tuple[int, int, int]] | None = None) -> dict:
    implemented = implemented if implemented is not None else implemented_draft()
    if tracked is None:
        tracked = tracked_versions()
    schema = classify_schema(*get(SCHEMA_URL))
    site = check_site(get, implemented)
    repo = check_repo(get, tracked, token if token is not None else os.environ.get(TOKEN_ENV))
    return {"schema_url": SCHEMA_URL, "implemented_draft": implemented, "schema": schema, "site": site,
            "repo": repo, "news": schema["news"] or site["news"] or repo["news"]}


def body_for(report: dict) -> str:
    lines = ["The scheduled HCP spec probe (`scripts/hcp_spec_probe.py`) found something to look at.", ""]
    schema, site, repo = report["schema"], report["site"], report["repo"]
    if schema["news"]:
        lines += [f"**The JSON Schema moved** — {SCHEMA_URL}: {schema['detail']}.", "", WHAT_CLOSES_2_1_1, ""]
    if site["news"]:
        lines += [f"**A newer draft** — {site['detail']}. Read what changed, then re-audit "
                  "`docs/architecture/hcp-conformance.md` against it (it names the draft it audits).", ""]
    if repo["news"] and repo["state"] == "newer-draft":
        lines += [f"**A newer draft in the spec repository** — {repo['detail']}. Read what changed "
                  "(`path-to-v2-final.md` lists the changes between drafts), then update "
                  "`docs/architecture/hcp-conformance.md` to name the draft it now models; that is "
                  "what quiets this probe.", ""]
    if repo["state"] == "no-access":
        lines += [f"**The spec repository cannot be read** — {repo['detail']}. Until it can, a new draft "
                  "there goes unnoticed.", ""]
    lines += ["```json", json.dumps(report, indent=2), "```"]
    return "\n".join(lines)


def title_for(report: dict) -> str:
    parts = []
    if report["schema"]["news"]:
        parts.append("the JSON Schema is published (closes conformance 2.1.1)")
    if report["site"]["news"]:
        parts.append("a newer draft is out")
    if report["repo"]["state"] == "no-access":
        parts.append("the spec repository cannot be read")
    elif report["repo"]["news"]:
        parts.append("a newer draft is in the spec repository")
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
