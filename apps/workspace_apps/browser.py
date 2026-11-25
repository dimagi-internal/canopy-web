"""Where an app's pages run in the browser extension: the manifest's `browser` key.

An app page may declare that the Dimagi Canopy extension shows it in its side panel
when the active tab's URL matches, and what it may ask of that tab's adapter (a
content script bundled in the extension, one per site). Design and wire protocol:
docs/architecture/browser-extension.md.

    "browser": [{"page": "gmail-panel", "matches": ["https://mail.google.com/*"],
                 "adapter": "gmail", "capabilities": ["read_open_email"],
                 "events": ["email_opened"]}]

The rules (`validate`):

* `page` is a `member` page of the same app — never a public or link page, so a
  page the panel draws is always drawn for a signed-in member, as on canopy.
* `adapter`, its `capabilities` and `events` are in `ADAPTERS`, the one registry of
  what the extension's adapters can do. It mirrors the extension's own adapters;
  adding one here is a deliberate act beside a new extension version.
* An adapter's `acts` are the capabilities that change the page (fill a compose, open
  one) rather than read it. This registry is the only source of that classification,
  never the extension's `hello`: the panel lets a page call an act only straight from
  a click on it (`frontend/src/ext/extensionBridge.ts::BrowserGate`), and a template
  update that adds one needs an admin (`template_diff.py`).
* `matches` are Chrome-style match patterns (`https://host/path*`), https only, and
  each host is within the adapter's `hosts`: a Gmail page can never be offered on
  another site, whatever it declares.

The canopy page that hosts the panel checks every bridge call against this
declaration and the tab's URL again before relaying it (`frontend/src/ext/`);
the matcher there mirrors `matches` here.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

#: What each adapter bundled in the extension can do. `hosts` bounds a page's `matches`;
#: `acts` are the `capabilities` that change the page (a click on the page is needed to call one).
ADAPTERS: dict[str, dict[str, tuple[str, ...]]] = {
    "gmail": {  # adapter v1.2
        "hosts": ("mail.google.com",),
        "capabilities": ("read_open_email", "read_open_thread", "compose_recipients", "start_compose", "fill_compose"),
        "acts": ("start_compose", "fill_compose"),
        "events": ("email_opened", "email_sent", "compose_recipients_changed"),
    },
}

for _name, _adapter in ADAPTERS.items():
    assert set(_adapter.get("acts", ())) <= set(_adapter["capabilities"]), f"{_name}: acts must be capabilities"


def acts_of(adapter: str, capabilities) -> list[str]:
    """Which of these capabilities of `adapter` are acts, by the registry (unknown adapter: none)."""
    acts = (ADAPTERS.get(adapter) or {}).get("acts", ())
    return [c for c in capabilities or [] if c in acts]

MAX_ENTRIES = 10
MAX_MATCHES = 10
MAX_URL = 2048

#: `https://<host>/<path>`: a host is a name, or `*.` and a name; the path may use `*`.
_PATTERN = re.compile(r"^https://(?P<host>(?:\*\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*)"
                      r"(?P<path>/[^\s#]*)$")


def _fail(where: str, why: str):
    raise ValueError(f"{where}: {why}")


def _host_within(host: str, allowed: str) -> bool:
    """Is a pattern's host (`a.b`, or `*.a.b`) inside an adapter host (`a.b`, or `*.a.b`)?"""
    if not allowed.startswith("*."):
        return host == allowed
    base = allowed[2:]
    bare = host[2:] if host.startswith("*.") else host
    return bare == base or bare.endswith("." + base)


def check_pattern(where: str, pattern, hosts: tuple[str, ...]) -> str:
    if not isinstance(pattern, str) or len(pattern) > 500:
        _fail(where, "must be a match pattern like https://mail.google.com/*")
    m = _PATTERN.match(pattern)
    if not m:
        _fail(where, "must be an https match pattern, https://<host>/<path> (`*` in the path, or `*.` before the host)")
    if not any(_host_within(m["host"], h) for h in hosts):
        _fail(where, f"'{m['host']}' is not a host of this adapter ({', '.join(hosts)})")
    return pattern


def validate(raw, pages: list[dict]) -> list[dict] | None:
    """The normalised `browser` declaration, None when absent, or ValueError naming what is wrong."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        _fail("browser", "must be a non-empty list of {page, matches, adapter, capabilities, events}")
    if len(raw) > MAX_ENTRIES:
        _fail("browser", f"at most {MAX_ENTRIES}")
    member_pages = {p["slug"] for p in pages if p["audience"] == "member"}
    out = []
    for i, spec in enumerate(raw):
        where = f"browser[{i}]"
        if not isinstance(spec, dict):
            _fail(where, "must be an object")
        unknown = set(spec) - {"page", "matches", "adapter", "capabilities", "events"}
        if unknown:
            _fail(where, f"unknown keys: {', '.join(sorted(unknown))}")
        page = spec.get("page")
        if page not in member_pages:
            _fail(f"{where}.page", f"'{page}' must be a member page of this app")
        name = spec.get("adapter")
        adapter = ADAPTERS.get(name) if isinstance(name, str) else None
        if adapter is None:
            _fail(f"{where}.adapter", f"must be one of: {', '.join(sorted(ADAPTERS))}")
        matches = spec.get("matches")
        if not isinstance(matches, list) or not matches or len(matches) > MAX_MATCHES:
            _fail(f"{where}.matches", f"must be a list of 1-{MAX_MATCHES} match patterns")
        norm = [check_pattern(f"{where}.matches[{j}]", p, adapter["hosts"]) for j, p in enumerate(matches)]
        lists = {}
        for kind in ("capabilities", "events"):
            got = spec.get(kind) or []
            if not isinstance(got, list) or any(not isinstance(x, str) for x in got):
                _fail(f"{where}.{kind}", "must be a list of names")
            bad = [x for x in got if x not in adapter[kind]]
            if bad:
                _fail(f"{where}.{kind}", f"{', '.join(bad)} not offered by '{name}' "
                                         f"(it offers: {', '.join(adapter[kind])})")
            lists[kind] = list(dict.fromkeys(got))
        out.append({"page": page, "matches": list(dict.fromkeys(norm)), "adapter": name, **lists})
    return out


def _glob(path: str) -> re.Pattern:
    return re.compile("^" + ".*".join(re.escape(part) for part in path.split("*")) + "$", re.DOTALL)


def matches(pattern: str, url: str) -> bool:
    """Chrome's match-pattern rule, for the https patterns `validate` admits: the host
    exactly (or any subdomain, for `*.`), on the default port, and the path plus query
    by glob. The fragment is never part of a match."""
    m = _PATTERN.match(pattern or "")
    if not m or not isinstance(url, str) or len(url) > MAX_URL:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or port not in (None, 443) or parts.username or parts.password:
        return False
    want = m["host"]
    if want.startswith("*."):
        if not (host == want[2:] or host.endswith(want[1:])):
            return False
    elif host != want:
        return False
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return bool(_glob(m["path"]).match(target))


def entries_for(manifest: dict, url: str) -> list[dict]:
    """The app's `browser` entries whose patterns match `url`, with their page."""
    pages = {p["slug"]: p for p in manifest.get("pages") or []}
    out = []
    for entry in manifest.get("browser") or []:
        page = pages.get(entry["page"])
        if page and page["audience"] == "member" and any(matches(p, url) for p in entry["matches"]):
            out.append({**entry, "acts": acts_of(entry["adapter"], entry["capabilities"]),
                        "page_title": page["title"], "role": page["role"]})
    return out
