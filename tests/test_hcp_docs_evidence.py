"""The HCP docs cannot cite what is not there.

`docs/architecture/hcp-conformance.md` is a self-audit: each row of it says "this
criterion is met, here is the evidence". `hcp-service.md` is the design record. An
audit whose evidence has been renamed away, or whose numbers no longer match the
code, is worse than none: it still reads as true. This test resolves what the two
docs cite, so a rename, a deleted test or a changed lifetime fails here, naming the
line to fix.

What is checked, by the shape of the backticked token (see `check`):

* a test name (`test_x`, or `test_x…` for a prefix) is a `def` in `tests/`;
* `hcp.x`, `hcp_api.x`, `hcp_oauth.x`, `embeddings.x` is an attribute of that module;
* a model, `Model.field` is a model in `apps/` with that attribute or field;
* a path (`apps/...`, `hcp/relevance.py`, `templates/...`, a bare `x.md`) is a file;
* a migration (`contacts/0022`) and a management command (`manage.py hcp_embed`)
  exist;
* a `GET /v1/grants` style route has a decorator; a bare URL path resolves;
* an audit event (`grant.expired`) is in `PersonAuditEvent.EVENT_TYPES`;
* a setting (`HCP_*`, `KB_*`) is in `config/settings` or the deploy template;
* anything else that looks like an identifier appears somewhere in the source.

Then `NUMBERS` pins the figures the docs state (lifetimes, limits, defaults) to the
constants that implement them. External things the docs name (another repo's file) are
in `EXTERNAL` with the reason.
"""
from __future__ import annotations

import datetime as dt
import importlib
import pathlib
import re

import pytest

pytestmark = pytest.mark.guard

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = [ROOT / "docs/architecture/hcp-conformance.md", ROOT / "docs/architecture/hcp-service.md"]
SOURCE_DIRS = ["apps", "config", "frontend/src", "deploy", "tests", "docs", "scripts", ".github"]
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".html", ".yaml", ".yml", ".md", ".sql"}
SKIP_DIRS = {"node_modules", "__pycache__", ".venv", "dist", "build", ".git"}

#: Named by the docs, but not in this repository.
EXTERNAL = {
    "connect_labs/labs/canopy.py": "the connect-labs repository (the site that signs the verified email)",
    "gcloud projects describe": "a shell command, not code",
}
#: Words the docs put in backticks that are protocol or product vocabulary, not symbols.
VOCABULARY = {
    "GET", "WHERE", "NUMBER", "Origin", "Retry-After", "HCP-Version", "HCP-Signature",
    "HCP-Delivery-Id", "HCP-v1-Core", "HCP-v1-Interop", "SA", "P", "eu", "global", "llm",
    "plain", "always", "default", "available", "person", "system", "user", "agent", "public",
    "internal", "version", "work_context", "locations/us", "us-discoveryengine.googleapis.com",
    "roles/discoveryengine.viewer", "canopy-494811", "canopy-web-task-role", "memory-embeddings",
    "bedrock:InvokeModel", "cacheConfig", "disableCache: true", "/ecs/labs-jj-canopy-web",
    "/canopy", "/", "*", "Authorization: Bearer hcpat_…", "<origin>/api/hcp", "hcp:{category}:{read|write}",
    "client:<client_id>", "client:<id>/<claim>", "contact:<pk>", "workspace:<slug>", "collection:<id>",
    "tree:<agent's root workspace>", "site=<name>", "hcpat_", "HttpOnly; Secure; SameSite=None; Partitioned",
    "Sec-Fetch-Site: same-origin", "<timestamp>.<body>", "https://labs.connect.dimagi.com/",
    "https://hcp.me/schemas/v1", "e.g.",
}

_TOKEN = re.compile(r"`([^`\n]+)`")
_FENCE = re.compile(r"```.*?```", re.S)


def _prose(path: pathlib.Path) -> str:
    return _FENCE.sub("", path.read_text(encoding="utf-8"))


def _tokens() -> list[tuple[str, str]]:
    out = []
    for doc in DOCS:
        for tok in sorted(set(_TOKEN.findall(_prose(doc)))):
            out.append((doc.name, tok.strip()))
    return out


def _files() -> list[pathlib.Path]:
    found = []
    for d in SOURCE_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and p.suffix in SOURCE_SUFFIXES and not (set(p.parts) & SKIP_DIRS):
                found.append(p)
    return found


@pytest.fixture(scope="module")
def corpus():
    files = _files()
    texts = {p: p.read_text(encoding="utf-8", errors="ignore") for p in files}
    posix = [p.relative_to(ROOT).as_posix() for p in files]
    commands = {p.stem for p in (ROOT / "apps").glob("*/management/commands/*.py")}
    test_names = set()
    for p, t in texts.items():
        if p.parts[-2] == "tests":
            test_names |= set(re.findall(r"^\s*(?:async )?def (test_\w+)", t, re.M))
    routes = []
    for p, t in texts.items():
        if p.suffix == ".py" and "apps" in p.parts:
            routes += [(m.group(1), m.group(2)) for m in
                       re.finditer(r"@\w*router\.(get|post|put|patch|delete)\(\s*\"([^\"]+)\"", t)]
    return {"files": posix, "texts": texts, "commands": commands, "tests": test_names,
            "routes": routes, "all_text": "\n".join(texts.values())}


def _norm_path(p: str) -> str:
    return re.sub(r"\{[^}]*\}|<[^>]*>", "{}", p.split("?")[0]).rstrip("/")


MODULES = {"hcp": "apps.contacts.hcp", "hcp_api": "apps.contacts.hcp_api",
           "hcp_oauth": "apps.contacts.hcp_oauth", "embeddings": "apps.contacts.embeddings"}
AUDIT_PREFIXES = ("grant.", "conflict.", "revocation.", "preference.", "collection.", "viewer.")


def check(tok: str, c: dict) -> str | None:
    """Why this token does not resolve, or None. Unrecognised shapes pass: this is a
    net for what the docs *cite*, not a parser of prose."""
    if tok in EXTERNAL or tok in VOCABULARY or "…" in tok and not tok.startswith("test_"):
        return None
    # A test name; a trailing ellipsis means "the test whose name starts so".
    if re.fullmatch(r"test_\w+…?", tok):
        stem = tok.rstrip("…")
        ok = any(n.startswith(stem) for n in c["tests"]) if tok.endswith("…") else stem in c["tests"]
        return None if ok else "no such test in tests/"
    # Test files.
    if re.fullmatch(r"(tests/)?test_[\w]+\.py", tok):
        name = tok if tok.startswith("tests/") else f"tests/{tok}"
        return None if name in c["files"] else "no such test file"
    if re.fullmatch(r"[\w]+\.test\.tsx?", tok):
        return None if any(f.endswith("/" + tok) for f in c["files"]) else "no such frontend test"
    # `module.symbol` or `module.symbol(args)`.
    m = re.match(r"(hcp|hcp_api|hcp_oauth|embeddings)\.([A-Za-z_]\w*)", tok)
    if m and m.group(2) != "py" and not tok.startswith("hcp.me"):
        mod = importlib.import_module(MODULES[m.group(1)])
        return None if hasattr(mod, m.group(2)) else f"{MODULES[m.group(1)]} has no {m.group(2)}"
    # A model, or `Model.attr`.
    m = re.fullmatch(r"([A-Z]\w+)(?:\.([a-z_*]\w*))?", tok)
    if m:
        from django.apps import apps

        models = {mm.__name__: mm for mm in apps.get_models()}
        if m.group(1) in models:
            attr = m.group(2)
            if not attr or "*" in attr:
                return None
            model = models[m.group(1)]
            names = {f.name for f in model._meta.get_fields()}
            return None if attr in names or hasattr(model, attr) else f"{m.group(1)} has no {attr}"
    # Audit events.
    if tok.startswith(AUDIT_PREFIXES) and re.fullmatch(r"[a-z]+\.[A-Za-z]+", tok):
        from apps.contacts.models import PersonAuditEvent

        return None if tok in PersonAuditEvent.EVENT_TYPES else "not in PersonAuditEvent.EVENT_TYPES"
    # Migrations.
    m = re.fullmatch(r"(\w+)/(\d{4})(?:_\w+)?", tok)
    if m:
        hits = list((ROOT / "apps" / m.group(1) / "migrations").glob(f"{m.group(2)}*.py"))
        return None if hits else "no such migration"
    if re.fullmatch(r"\d{4}", tok):
        return None if list((ROOT / "apps/contacts/migrations").glob(f"{tok}*.py")) else "no such migration"
    # Management commands.
    m = re.fullmatch(r"manage\.py (\w+)", tok)
    if m:
        return None if m.group(1) in c["commands"] else "no such management command"
    # Routes: `METHOD /path`.
    m = re.fullmatch(r"(GET|POST|PUT|PATCH|DELETE) (/\S*)", tok)
    if m:
        want = _norm_path(m.group(2))
        for method, path in c["routes"]:
            have = _norm_path(path)
            if have and method == m.group(1).lower() and (want == have or want.endswith(have)):
                return None
        return "no route decorator with that method and path"
    # Settings and environment names.
    m = re.fullmatch(r"((?:HCP|KB)_[A-Z_]+)(?:=.*)?", tok)
    if m:
        name = m.group(1)
        for f in c["files"]:
            if (f.startswith("config/settings") or f.startswith("deploy/")) and name in c["texts"][ROOT / f]:
                return None
        return "not a setting in config/settings or deploy/"
    m = re.fullmatch(r"settings\.([A-Z_]+)", tok)
    if m:
        return None if m.group(1) in c["texts"][ROOT / "config/settings/base.py"] else "not a setting"
    # URL paths (bare).
    if tok.startswith("/") and re.fullmatch(r"/[\w./-]*", tok) and not tok.endswith("/"):
        return _url(tok, c)
    # Files.
    if re.fullmatch(r"[\w./*-]+\.(py|tsx|ts|html|yaml|yml|md)", tok) or tok.startswith(
            ("apps/", "templates/", "frontend/", "deploy/")):
        if "*" in tok:
            pat = re.compile(re.escape(tok).replace(r"\*", "[^/]*") + "$")
            return None if any(pat.search(f) for f in c["files"]) or any(
                pat.search(f.rstrip("/")) for f in c["files"]) else "no file matches"
        t = tok.rstrip("/")
        return None if any(f == t or f.endswith("/" + t) or f.startswith(t + "/") or ("/" + t + "/") in f
                           for f in c["files"]) else "no such file"
    # Bare module names: hcp_api, hcp_collections, hcp_oauth_views.
    if re.fullmatch(r"hcp_\w+", tok) and (ROOT / "apps/contacts" / f"{tok}.py").exists():
        return None
    if re.fullmatch(r"hcp_\w+", tok) and tok in c["commands"]:
        return None
    # Private helpers named bare (`_ok`, `_problem`).
    if re.fullmatch(r"_\w+", tok):
        return None if re.search(rf"(def|class) {tok}\b|^{tok}\b", c["all_text"], re.M) else "not defined"
    # Everything else shaped like an identifier must at least still occur in the source.
    ident = re.match(r"[A-Za-z_][\w.:-]*", tok)
    if ident and len(ident.group(0)) > 4 and ident.group(0) == tok.split("(")[0].split("=")[0].strip():
        return None if ident.group(0) in c["all_text"] else "no longer appears anywhere in the source"
    return None


def _url(path: str, c: dict) -> str | None:
    from django.urls import Resolver404, resolve

    if path == "/hcp-clients":
        return None if "'/hcp-clients'" in c["texts"][ROOT / "frontend/src/router.tsx"] else "no SPA route"
    if path.startswith(("/people/", "/v1/", "/api/embed", "/api/me", "/oauth")) or path in {"/v1/audit"}:
        return None   # relative or family names, checked through their routes above
    try:
        resolve(path)
        return None
    except Resolver404:
        return "does not resolve to a view"


def test_every_token_the_hcp_docs_cite_resolves(corpus):
    bad = []
    seen = 0
    for doc, tok in _tokens():
        why = check(tok, corpus)
        seen += 1
        if why:
            bad.append(f"  {doc}: `{tok}` — {why}")
    assert seen > 200, "the docs lost their citations, or the token pattern broke"
    assert not bad, (
        "The HCP docs cite things that are not in the code. Fix the DOC to the code (or, if the code\n"
        "is wrong, fix it with a failing test first):\n" + "\n".join(bad))


def test_the_checker_catches_rot(corpus):
    """The net itself: a made-up test, symbol, file, route and event all fail."""
    for tok in ("test_a_thing_nobody_wrote", "hcp.no_such_function", "HcpClient.no_such_field",
                "apps/contacts/hcp_missing.py", "GET /v1/nothing-here", "grant.imagined",
                "manage.py hcp_imagined", "contacts/0999", "HCP_NOT_A_SETTING"):
        assert check(tok, corpus), tok
    for tok in ("test_pkce_is_required", "hcp.reach_of", "HcpClient.allowed_scopes", "hcp_oauth.py",
                "GET /v1/grants", "grant.expired", "manage.py hcp_embed", "contacts/0022", "HCP_PEOPLE"):
        assert check(tok, corpus) is None, tok


def test_the_conformance_table_cites_tests_it_can_run(corpus):
    """Every evidence cell that names a test names one that exists; and the audit
    cites at least the tests of the OAuth gates it claims."""
    text = _prose(ROOT / "docs/architecture/hcp-conformance.md")
    cited = set(re.findall(r"`(test_\w+)…?`", text))
    assert len(cited) >= 30
    for must in ("test_pkce_is_required", "test_a_code_works_once",
                 "test_a_rotated_refresh_token_presented_again_revokes_the_grant",
                 "test_a_temporary_grant_is_not_refreshed_past_its_expiry"):
        assert must in cited, f"hcp-conformance.md's OAuth table no longer cites {must}"


# --- the numbers the docs state ----------------------------------------------------------------


def _numbers():
    from django.conf import settings

    from apps.contacts import hcp, hcp_oauth

    h = dt.timedelta
    return [
        # (doc, text that must appear, what the code says, the code value that makes it true)
        ("hcp-service.md", "access 1 h", hcp_oauth.ACCESS_TTL, h(hours=1)),
        ("hcp-conformance.md", "access 1 h", hcp_oauth.ACCESS_TTL, h(hours=1)),
        ("hcp-conformance.md", "refresh 30 d persistent", hcp_oauth.REFRESH_TTL_PERSISTENT, h(days=30)),
        ("hcp-conformance.md", "codes 10 min", hcp_oauth.CODE_TTL, h(minutes=10)),
        ("hcp-service.md", "(10 min, single use)", hcp_oauth.CODE_TTL, h(minutes=10)),
        ("hcp-service.md", "held server-side for 15 minutes", hcp_oauth.PENDING_TTL, h(minutes=15)),
        ("hcp-service.md", "1 h, 4 h (pre-selected), 24 h",
         tuple(hcp_oauth.TEMPORARY_CHOICES.values()), (h(hours=1), h(hours=4), h(hours=24))),
        ("hcp-service.md", "4 h (pre-selected)", hcp_oauth.TEMPORARY_DEFAULT, "4h"),
        ("hcp-service.md", "1, 5, 15, 30 and 60 minutes",
         tuple(hcp_oauth.WEBHOOK_BACKOFF), tuple(h(minutes=m) for m in (1, 5, 15, 30, 60))),
        ("hcp-service.md", "6 attempts", hcp_oauth.WEBHOOK_MAX_ATTEMPTS, 6),
        ("hcp-service.md", "120 requests a minute per client", hcp_oauth.RATE_LIMIT_PER_MINUTE, 120),
        ("hcp-conformance.md", "min(maxEntries, 20)", hcp.MAX_ENTRIES, 20),
        ("hcp-service.md", "at most 20 entries per search", hcp.MAX_ENTRIES, 20),
        ("hcp-service.md", "`ROLE_FLOOR` (2)", hcp.relevance.ROLE_FLOOR, 2),
        ("hcp-service.md", "| `HCP_SEMANTIC_FLOOR` | 0.60 |", settings.HCP_SEMANTIC_FLOOR, 0.60),
        ("hcp-service.md", "| `HCP_SEMANTIC_WEIGHT` | 4.0 |", settings.HCP_SEMANTIC_WEIGHT, 4.0),
        ("hcp-service.md", "| `HCP_PROJECT_BOOST` | 2.0 |", settings.HCP_PROJECT_BOOST, 2.0),
        ("hcp-service.md", "every 15 minutes", importlib.import_module("apps.contacts.embeddings").RETENTION_TTL, 900),
        ("hcp-conformance.md", "`conformance_level: HCP-v1-Core`", hcp.CONFORMANCE_LEVEL, "HCP-v1-Core"),
    ]


def test_the_numbers_the_docs_state_are_the_codes():
    wrong = []
    for doc, snippet, actual, expected in _numbers():
        text = (ROOT / "docs/architecture" / doc).read_text(encoding="utf-8")
        if snippet not in text:
            wrong.append(f"  {doc} no longer says {snippet!r}: update this test with the doc")
        if actual != expected:
            wrong.append(f"  {doc} says {snippet!r} but the code has {actual!r}")
    assert not wrong, "The HCP docs and the code disagree on a figure:\n" + "\n".join(wrong)


def test_the_docs_describe_the_deployed_defaults():
    """The knobs table shows the code's defaults; the deploy template sets the same."""
    cfn = (ROOT / "deploy/aws/canopy-web.cfn.yaml").read_text(encoding="utf-8")
    for name, value in (("HCP_SEMANTIC_FLOOR", "0.60"), ("HCP_SEMANTIC_WEIGHT", "4.0"),
                        ("HCP_PROJECT_BOOST", "2.0")):
        assert re.search(rf"Name: {name}, Value: \"{re.escape(value)}\"", cfn), name
