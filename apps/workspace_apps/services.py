"""The app store, installing, the draft → test → publish life cycle, and using an app.

Routes are in `api.py`. Every read and write goes through here with the caller's
ROLES already worked out (`access.roles_for`) and a `space` — "live", or one
draft's private test copy — so the rule is in one place whichever door (a member
page, a public page, an MCP tool, a scenario run) the request came through.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import date, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings as django_settings
from django.core import signing
from django.core.mail import EmailMessage
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.canopy_sessions import attachment_storage

from . import manifest as mf
from . import overlay as ov
from .models import (
    LIVE,
    AppFile,
    AppInstallation,
    AppRecord,
    AppRecordEvent,
    AppScheduledEmail,
    AppTemplate,
    AppTemplateVersion,
    AppVersion,
)
from .scope import Scope

BUNDLED_DIR = Path(__file__).parent / "bundled"
MAX_QUERY_LIMIT = 2000
#: The longest an action's email may be held back (two weeks).
MAX_DELAY_HOURS = 14 * 24
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)\s*\}\}")


class Refused(Exception):
    """The caller may not do this. `status` is the HTTP answer: 404 when saying
    why would reveal what exists, 403 otherwise, 409 for a state conflict."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


class TooManyRequests(Exception):
    pass


# --- the store ------------------------------------------------------------------------


def _read_bundled(key: str) -> dict:
    """A bundled app's manifest, its pages' code read from `pages/<slug>.jsx`."""
    root = BUNDLED_DIR / key
    path = root / "manifest.json"
    if not mf.KEY.match(key) or not path.is_file():
        raise LookupError(f"no bundled app '{key}'")
    raw = json.loads(path.read_text())
    for page in raw.get("pages", []):
        if "code" not in page:
            page["code"] = (root / "pages" / f"{page['slug']}.jsx").read_text()
    for fixtures in ("fixtures", "scenarios"):
        f = root / f"{fixtures}.json"
        if f.is_file() and fixtures not in raw:
            raw[fixtures] = json.loads(f.read_text())
    return mf.validate(raw)


def bundled_manifest(key: str) -> dict:
    return _read_bundled(key)


def sync_bundled() -> None:
    """Make the store hold every bundled app's current version. A released
    version never changes: shipping a change means bumping `version`."""
    for d in sorted(BUNDLED_DIR.iterdir()):
        if not (d / "manifest.json").is_file():
            continue
        m = _read_bundled(d.name)
        want = {"name": m["name"], "description": m["description"], "owner_id": None, "listed": True}
        tmpl = AppTemplate.objects.filter(key=m["key"]).first()
        # Write only what changed: the store is read on every visit, and a read must not take a write lock.
        if tmpl is None or any(getattr(tmpl, k) != v for k, v in want.items()):
            tmpl, _ = AppTemplate.objects.update_or_create(key=m["key"], defaults=want)
        if not tmpl.versions.filter(version=m["version"]).exists():
            AppTemplateVersion.objects.get_or_create(template=tmpl, version=m["version"],
                                                     defaults={"manifest": m, "changelog": m["changelog"]})


def _version_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", v or "0"))


def latest_version(tmpl: AppTemplate) -> AppTemplateVersion:
    return max(tmpl.versions.all(), key=lambda v: (_version_key(v.version), v.released_at))


def store(workspace_id: str) -> list[dict]:
    """The templates this workspace may install: canopy's, and its own."""
    sync_bundled()
    rows = AppTemplate.objects.filter(Q(owner__isnull=True, listed=True) | Q(owner_id=workspace_id))
    out = []
    for t in rows.prefetch_related("versions"):
        v = latest_version(t)
        listing = v.manifest.get("listing") or {}
        shots = listing.get("screenshots") or [] if t.owner_id is None else []
        out.append({"key": t.key, "name": t.name, "description": t.description, "version": v.version,
                    "publisher": "canopy" if t.owner_id is None else t.owner_id, "changelog": v.changelog,
                    "tagline": listing.get("tagline") or "", "cover": shots[0]["file"] if shots else ""})
    return out


def _store_template(workspace_id: str, key: str, *, sync: bool = True) -> AppTemplate:
    if sync:
        sync_bundled()
    t = AppTemplate.objects.filter(key=key).filter(Q(owner__isnull=True, listed=True) | Q(owner_id=workspace_id)).first()
    if t is None:
        raise LookupError(f"no app '{key}' in the store")
    return t


def store_detail(workspace_id: str, key: str) -> dict:
    """One store app, as someone deciding whether to install it reads it: the
    listing, what it adds (pages, roles, records, settings, retention), what it
    shows the public, and every version's changelog."""
    t = _store_template(workspace_id, key)
    v = latest_version(t)
    m = v.manifest
    listing = m.get("listing") or {}
    if t.owner_id is not None:
        listing = {**listing, "screenshots": []}
    caps = {"content.write": "every editor", "members.manage": "workspace admins", "integrations": "workspace admins",
            "read": "every member"}
    roles = [{"name": "maintainer", "description": "Customizes the app, tests changes on a copy and publishes them.",
              "holders": "workspace admins, and anyone given it by name"}]
    roles += [{"name": n, "description": r.get("description") or "",
               "holders": (caps.get(r["capability"], r["capability"]) + ", and anyone given it by name")
               if r.get("capability") else "only people given it by name"} for n, r in (m.get("roles") or {}).items()]
    pages = [{"title": p["title"], "audience": p["audience"]} for p in m.get("pages") or []
             if p.get("nav", True) or p["audience"] != "member"]
    records = [{"label": c.get("label") or n, "fields": len(c["fields"]),
                "restricted": sorted({f["read"] for f in c["fields"].values() if f.get("read")}),
                "scoped": "scope" in c} for n, c in m["collections"].items()]
    retention = [str(r.get("label") or r.get("collection") or "") for r in m.get("retention") or []]
    versions = sorted(t.versions.all(), key=lambda x: (_version_key(x.version), x.released_at), reverse=True)
    return {"key": t.key, "name": t.name, "description": t.description, "version": v.version,
            "publisher": "canopy" if t.owner_id is None else t.owner_id, "listing": listing, "roles": roles,
            "pages": pages, "records": records, "settings": len(m.get("settings") or {}), "retention": retention,
            "public_pages": [p["title"] for p in pages if p["audience"] == "public"],
            "emails": sorted({a["label"] for a in m["actions"].values() if a.get("effects")}),
            "versions": [{"version": x.version, "released_at": x.released_at.isoformat(), "changelog": x.changelog}
                         for x in versions[:10]],
            "installed": AppInstallation.objects.filter(workspace_id=workspace_id, template=t).exists()}


def store_screenshot(workspace_id: str, key: str, file: str) -> tuple[bytes, str]:
    """A bundled template's screenshot, by the name its listing gives it."""
    t = _store_template(workspace_id, key, sync=False)  # read-only: a page of images must not write
    listing = latest_version(t).manifest.get("listing") or {}
    if t.owner_id is not None or file not in {s["file"] for s in listing.get("screenshots") or []}:
        raise LookupError("no such screenshot")
    path = (BUNDLED_DIR / t.key / file).resolve()
    if BUNDLED_DIR.resolve() not in path.parents or not path.is_file():
        raise LookupError("no such screenshot")
    kind = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[path.suffix.lstrip(".")]
    return path.read_bytes(), kind


# --- installing --------------------------------------------------------------------------


@transaction.atomic
def install(*, workspace_id: str, user, template: str = "", manifest: dict | None = None) -> AppInstallation:
    """Install a store template, or a manifest the workspace wrote (which becomes
    its own unlisted template, so it has versions and upgrades like any other)."""
    if bool(template) == bool(manifest):
        raise ValueError("install a template from the store (`template`) or your own (`manifest`), not both")
    sync_bundled()
    who = user if getattr(user, "is_authenticated", False) else None
    if manifest:
        m = mf.validate(manifest)
        tkey = f"{workspace_id}/{m['key']}"
        if AppTemplate.objects.filter(key=tkey).exists():
            raise ValueError(f"'{m['key']}' already exists in this workspace's templates")
        tmpl = AppTemplate.objects.create(key=tkey, name=m["name"], description=m["description"],
                                          owner_id=workspace_id, listed=False)
        tv = AppTemplateVersion.objects.create(template=tmpl, version=m["version"] or "1", manifest=m,
                                               changelog=m["changelog"])
    else:
        tmpl = AppTemplate.objects.filter(key=template).first()
        if tmpl is None or (tmpl.owner_id is not None and tmpl.owner_id != workspace_id):
            raise LookupError(f"no app '{template}' in the store")
        tv = latest_version(tmpl)
    key = tv.manifest["key"]
    if AppInstallation.objects.filter(workspace_id=workspace_id, key=key).exists():
        raise ValueError(f"'{key}' is already installed in this workspace")
    inst = AppInstallation.objects.create(workspace_id=workspace_id, key=key, template=tmpl, name=tv.manifest["name"],
                                          installed_by=who)
    v = AppVersion.objects.create(installation=inst, number=1, template_version=tv, overlay=[],
                                  status=AppVersion.PUBLISHED, origin="install", created_by=who, published_by=who,
                                  published_at=timezone.now(), note="installed")
    _make_live(inst, v)
    return inst


def _make_live(inst: AppInstallation, v: AppVersion) -> None:
    m, _ = ov.compose(v.template_version.manifest, v.overlay)
    inst.live, inst.manifest, inst.name = v, m, m["name"]
    inst.save()


def manifest_for(inst: AppInstallation, space: str) -> dict:
    """The manifest a request in `space` runs: the live one, or a draft's."""
    if space == LIVE:
        return inst.manifest
    v = draft_for_space(inst, space)
    return effective(v)[0]


def draft_for_space(inst: AppInstallation, space: str) -> AppVersion:
    m = re.fullmatch(r"draft-(\d+)", space or "")
    v = inst.versions.filter(pk=int(m.group(1)), status=AppVersion.DRAFT).first() if m else None
    if v is None:
        raise Refused("that draft is no longer open", status=404)
    return v


def effective(v: AppVersion) -> tuple[dict, list]:
    return ov.compose(v.template_version.manifest, v.overlay)


def upgrade_available(inst: AppInstallation) -> AppTemplateVersion | None:
    newest = latest_version(inst.template)
    current = inst.live.template_version if inst.live_id else None
    return newest if current and _version_key(newest.version) > _version_key(current.version) else None


# --- drafts ------------------------------------------------------------------------------


def open_draft(inst: AppInstallation) -> AppVersion | None:
    return inst.versions.filter(status=AppVersion.DRAFT).first()


@transaction.atomic
def start_draft(inst: AppInstallation, *, user, upgrade: bool = False, note: str = "") -> AppVersion:
    """A new draft from the live version: the same overlay, over the live template
    version — or, for an upgrade, over the newest one. Its test copy is seeded
    with the template's sample records, never with live data."""
    if open_draft(inst) is not None:
        raise Refused("this app already has a draft open — test and publish it, or discard it", status=409)
    live = inst.live
    tv = live.template_version
    if upgrade:
        tv = upgrade_available(inst)
        if tv is None:
            raise Refused("the app is on the newest template version already", status=409)
    number = (inst.versions.order_by("-number").values_list("number", flat=True).first() or 0) + 1
    v = AppVersion.objects.create(installation=inst, number=number, template_version=tv,
                                  overlay=copy.deepcopy(live.overlay), status=AppVersion.DRAFT,
                                  origin="upgrade" if upgrade else "edit", note=note[:300],
                                  created_by=user if getattr(user, "is_authenticated", False) else None)
    seed(inst, v.space, effective(v)[0])
    return v


@transaction.atomic
def discard_draft(v: AppVersion) -> None:
    _clear_space(v.installation, v.space)
    v.status = AppVersion.DISCARDED
    v.save(update_fields=["status"])


def _clear_space(inst: AppInstallation, space: str) -> None:
    if space == LIVE:  # pragma: no cover — guarded by callers
        raise ValueError("never clear the live space")
    AppRecordEvent.objects.filter(installation=inst, space=space).delete()
    AppRecord.objects.filter(installation=inst, space=space).delete()
    AppFile.objects.filter(installation=inst, space=space).delete()


def _require_draft(v: AppVersion) -> None:
    if v.status != AppVersion.DRAFT:
        raise Refused("only a draft can be changed", status=409)


@transaction.atomic
def add_op(v: AppVersion, raw: dict, *, by: str) -> dict:
    """Validate one change against the draft and append it, or raise OpError
    naming exactly why it cannot apply."""
    _require_draft(v)
    v = AppVersion.objects.select_for_update().get(pk=v.pk)
    op = ov.new_op(raw, by=by)
    ov.check(v.template_version.manifest, v.overlay, op)
    v.overlay = [*v.overlay, op]
    v.checks = {}
    v.save(update_fields=["overlay", "checks"])
    return op


@transaction.atomic
def remove_op(v: AppVersion, op_id: str) -> None:
    _require_draft(v)
    v = AppVersion.objects.select_for_update().get(pk=v.pk)
    kept = [o for o in v.overlay if o.get("id") != op_id]
    if len(kept) == len(v.overlay):
        raise Refused(f"no change {op_id} in this draft", status=404)
    v.overlay, v.checks = kept, {}
    v.save(update_fields=["overlay", "checks"])


def review(v: AppVersion) -> dict:
    """Everything a person needs to decide on a version, in plain words: what it
    changes, what needs an admin, what the template changed, what was set aside."""
    m, quarantine = effective(v)
    live = v.installation.live
    live_ids = {o["id"] for o in (live.overlay if live else [])}
    changes = []
    for op in v.overlay:
        if any(q["op"]["id"] == op["id"] for q in quarantine):
            continue
        changes.append({"id": op["id"], "op": op["op"], "summary": ov.describe(op, m), "intent": op.get("intent", ""),
                        "by": op.get("by", ""), "at": op.get("at", ""), "new": op["id"] not in live_ids,
                        "risk": ov.is_risky(op, m)})
    template_change = None
    if live and v.template_version_id != live.template_version_id:
        template_change = {"from": live.template_version.version, "to": v.template_version.version,
                           "changelog": v.template_version.changelog}
    forked = [p["slug"] for p in m["pages"] if p.get("forked")]
    return {
        "changes": changes,
        "risks": [c for c in changes if c["risk"] and c["new"]],
        "quarantine": [{"id": q["op"]["id"], "summary": ov.describe(q["op"], m), "intent": q["op"].get("intent", ""),
                        "reason": q["reason"]} for q in quarantine],
        "template_change": template_change,
        "forked_pages": forked,
        "checks": v.checks or {},
    }


# --- scenarios: the checks a version passes before it goes live -----------------------------


def _resolve(value, saved: dict):
    if isinstance(value, str) and value.startswith("@"):
        if value[1:] not in saved:
            raise ValueError(f"nothing saved as {value}")
        return saved[value[1:]]
    if isinstance(value, dict):
        return {k: _resolve(x, saved) for k, x in value.items()}
    return value


def run_checks(v: AppVersion) -> dict:
    """Run every scenario of the version's composed manifest on a scratch copy
    (seeded with the sample records, thrown away after), and record the result."""
    m, quarantine = effective(v)
    inst = v.installation
    results = []
    for sc in m["scenarios"]:
        space = f"check-{v.pk}-{uuid.uuid4().hex[:8]}"
        try:
            with transaction.atomic():
                saved = seed(inst, space, m)
                _run_scenario(inst, m, space, sc, saved)
                results.append({"name": sc["name"], "ok": True, "origin": sc.get("origin", "template")})
                transaction.set_rollback(True)
        except Exception as exc:  # a failing step IS the result
            results.append({"name": sc["name"], "ok": False, "error": str(exc)[:500],
                            "origin": sc.get("origin", "template")})
    checks = {"ok": all(r["ok"] for r in results) and not quarantine, "ran_at": timezone.now().isoformat(),
              "results": results, "quarantined": len(quarantine)}
    v.checks = checks
    v.save(update_fields=["checks"])
    return checks


def _run_scenario(inst, m, space, sc, saved):
    for i, st in enumerate(sc["steps"], 1):
        if "act" in st:
            spec = m["actions"][st["act"]]
            role = st.get("as", spec["role"])
            roles = {"public"} if role == "public" else {"public", "member", role}
            record = _resolve(st.get("record"), saved)
            # A link action runs as the holder of that record's link.
            link = {"name": spec["link"], "record": record} if spec["role"] == "link" else None
            try:
                out = act(inst, roles=roles, action=st["act"], record_id=record,
                          input=_resolve(st.get("input") or {}, saved), user=None, space=space, manifest=m,
                          public=role == "public", label=st.get("actor") or "scenario",
                          params=_resolve(st.get("params") or {}, saved), link=link)
            except (Refused, ValueError) as exc:
                if st.get("expect_error"):
                    continue
                raise ValueError(f"step {i} ({st['act']}): {exc}") from exc
            if st.get("expect_error"):
                raise ValueError(f"step {i} ({st['act']}) should have been refused")
            if st.get("save"):
                saved[st["save"]] = out["id"]
        else:
            ex = st["expect"]
            rows = query(inst, roles={"public", "member", *m["roles"]}, collection=ex["collection"],
                         where=_resolve(ex.get("where") or {}, saved), limit=MAX_QUERY_LIMIT, space=space, manifest=m,
                         public=bool(ex.get("public")))
            want = ex.get("count", 1)
            if len(rows) != want:
                raise ValueError(f"step {i}: expected {want} {ex['collection']} matching {ex.get('where') or {}}, "
                                 f"found {len(rows)}")


# --- publishing ----------------------------------------------------------------------------


def publish(v: AppVersion, *, user, may_approve_risk: bool) -> AppVersion:
    """Make a draft the live version. Refused while anything is quarantined or a
    check fails; a risky change needs someone who may approve it. The checks run
    (and their result is kept) before the publish itself, so a refusal still
    leaves the person the failures to read."""
    _require_draft(v)
    r = review(v)
    if r["quarantine"]:
        raise Refused("some changes no longer fit this template version — fix or remove them first", status=409)
    if r["risks"] and not may_approve_risk:
        raise Refused("this draft " + "; ".join(sorted({x["risk"] for x in r["risks"]}))
                      + " — a workspace admin must publish it")
    checks = run_checks(v)
    if not checks["ok"]:
        failed = [x["name"] for x in checks["results"] if not x["ok"]]
        raise Refused(f"checks failed: {', '.join(failed)}", status=409)
    return _publish(v, user=user)


@transaction.atomic
def _publish(v: AppVersion, *, user) -> AppVersion:
    inst = AppInstallation.objects.select_for_update().get(pk=v.installation_id)
    v = AppVersion.objects.select_for_update().get(pk=v.pk)
    _require_draft(v)
    space = v.space
    if inst.live_id:
        AppVersion.objects.filter(pk=inst.live_id).update(status=AppVersion.SUPERSEDED)
    v.status, v.published_by, v.published_at = AppVersion.PUBLISHED, user, timezone.now()
    v.save(update_fields=["status", "published_by", "published_at"])
    _clear_space(inst, space)
    _make_live(inst, v)
    return v


@transaction.atomic
def rollback(inst: AppInstallation, *, number: int, user) -> AppVersion:
    """Publish an earlier version's overlay, over ITS template version, as a new
    version: history only grows. Records are untouched — overlays only add, so
    an earlier version reads today's records fine."""
    if open_draft(inst) is not None:
        raise Refused("discard the open draft before rolling back", status=409)
    old = inst.versions.filter(number=number).exclude(status=AppVersion.DRAFT).first()
    if old is None:
        raise Refused(f"no version {number}", status=404)
    nxt = (inst.versions.order_by("-number").values_list("number", flat=True).first() or 0) + 1
    if inst.live_id:
        AppVersion.objects.filter(pk=inst.live_id).update(status=AppVersion.SUPERSEDED)
    v = AppVersion.objects.create(installation=inst, number=nxt, template_version=old.template_version,
                                  overlay=copy.deepcopy(old.overlay), status=AppVersion.PUBLISHED, origin="rollback",
                                  note=f"rolled back to version {number}", created_by=user, published_by=user,
                                  published_at=timezone.now())
    _make_live(inst, v)
    return v


# --- test copies ---------------------------------------------------------------------------


def seed(inst: AppInstallation, space: str, m: dict) -> dict:
    """Write the manifest's sample records into `space`. Returns {alias: id}."""
    saved: dict[str, int] = {}
    for coll, rows in (m.get("fixtures") or {}).items():
        if coll not in m["collections"]:
            continue
        fields = m["collections"][coll]["fields"]
        for row in rows:
            data = {}
            for k, val in row.items():
                if k.startswith("_") or k not in fields:
                    continue
                val = _resolve(val, saved)
                if fields[k]["type"] == "ref":
                    data[k] = int(val)
                elif fields[k]["type"] != "file":
                    data[k] = _coerce(k, fields[k], val, inst, space)
            for k, f in fields.items():
                if data.get(k) is None and "default" in f:
                    data[k] = f["default"]
            rec = AppRecord.objects.create(installation=inst, space=space, collection=coll,
                                           data={k: v for k, v in data.items() if v is not None},
                                           created_by_label="sample data")
            if row.get("_as"):
                saved[row["_as"]] = rec.pk
    return saved


# --- reads -------------------------------------------------------------------------------


def readable(spec: dict, roles: set[str] | None) -> list[str]:
    """The fields of a collection these roles may read: a field with its own
    `read` role is left out for everyone without it (`roles=None` = the app itself)."""
    return [k for k, f in spec["fields"].items() if roles is None or not f.get("read") or f["read"] in roles]


def may_read(spec: dict, roles: set[str]) -> bool:
    """The collection's `read` role, or one of its `read_also` roles."""
    return spec["read"] in roles or bool(roles & set(spec.get("read_also") or []))


def scope_for(inst: AppInstallation, m: dict, space: str, user, roles: set[str]) -> Scope | None:
    """The record-level narrowing for a member's session (scope.py), or None for
    the app itself, a visitor and a link holder, which are not scoped."""
    return Scope(inst, m, space, user, roles) if getattr(user, "is_authenticated", False) else None


def _row(rec: AppRecord, fields: list[str] | None = None) -> dict:
    data = rec.data if fields is None else {k: rec.data[k] for k in fields if k in rec.data}
    return {"id": rec.pk, "created_at": rec.created_at.isoformat(), "updated_at": rec.updated_at.isoformat(), **data}


def _matches(data: dict, where: dict) -> bool:
    """A record's data against a {field: value | [values]} guard."""
    for k, v in where.items():
        if isinstance(v, list):
            if data.get(k) not in v:
                return False
        elif data.get(k) != v:
            return False
    return True


def query(inst: AppInstallation, *, roles: set[str], collection: str, where: dict | None = None,
          order: str = "-created_at", limit: int = 100, public: bool = False, space: str = LIVE,
          manifest: dict | None = None, user=None) -> list[dict]:
    """Records of one collection the caller may read. `public=True` is the
    anonymous door: the collection's public projection, its `where` always
    applied on top of the caller's and only its fields returned."""
    m = manifest or manifest_for(inst, space)
    spec = m["collections"].get(collection)
    if spec is None:
        raise ValueError(f"no collection '{collection}'")
    where = dict(where or {})
    if public:
        pub = spec.get("public")
        if pub is None:
            raise Refused(f"'{collection}' is not public", status=404)
        # Both apply: a caller asking for what the projection excludes gets nothing.
        if any(k in where and where[k] != v for k, v in pub["where"].items()):
            return []
        where.update(pub["where"])
        projection = pub["fields"]
    elif not may_read(spec, roles):
        raise Refused(f"reading '{collection}' needs the '{spec['read']}' role")
    else:
        projection = readable(spec, roles)
    fields = spec["fields"]
    qs = AppRecord.objects.filter(installation=inst, space=space, collection=collection)
    sc = None if public else scope_for(inst, m, space, user, roles)
    ids = sc.allowed(collection) if sc else None
    if ids is not None:
        qs = qs.filter(pk__in=ids)
    for k, v in where.items():
        if k == "id":
            qs = qs.filter(pk__in=v) if isinstance(v, list) else qs.filter(pk=v)
            continue
        if k not in fields:
            raise ValueError(f"'{k}' is not a field of '{collection}'")
        if k not in projection and not (public and k in spec["public"]["where"]):
            raise ValueError(f"'{k}' is not a field you may read on '{collection}'")
        if isinstance(v, list):
            q = Q()
            for one in v[:50]:
                q |= Q(**{f"data__{k}": one})
            qs = qs.filter(q)
        else:
            qs = qs.filter(**{f"data__{k}": v})
    desc = order.startswith("-")
    name = order.lstrip("-")
    if name in ("created_at", "updated_at"):
        qs = qs.order_by(order, "-id")
    elif name in fields and name in projection:
        qs = qs.order_by((f"-data__{name}" if desc else f"data__{name}"), "-id")
    else:
        raise ValueError(f"cannot order by '{order}'")
    limit = max(1, min(int(limit or 100), MAX_QUERY_LIMIT))
    return [_row(r, projection) for r in qs[:limit]]


def history(inst: AppInstallation, *, roles: set[str], record_id: int, space: str = LIVE, user=None) -> list[dict]:
    m = manifest_for(inst, space)
    rec = AppRecord.objects.filter(installation=inst, space=space, pk=record_id).first()
    if rec is None:
        raise Refused(f"record {record_id} not found", status=404)
    spec = m["collections"].get(rec.collection)
    if spec is None or not may_read(spec, roles):
        raise Refused(f"record {record_id} not found", status=404)
    sc = scope_for(inst, m, space, user, roles)
    if sc and not sc.sees(rec.collection, rec.pk):
        raise Refused(f"record {record_id} not found", status=404)
    shown = set(readable(spec, roles))
    return [{"action": e.action, "op": e.op, "changes": {k: v for k, v in (e.changes or {}).items() if k in shown},
             "effects": e.effects, "actor": e.actor_label, "at": e.at.isoformat()} for e in rec.events.all()]


# --- writes ------------------------------------------------------------------------------


def _coerce(name: str, spec: dict, value, inst: AppInstallation, space: str):
    """One field's value, validated against its declaration (None = empty)."""
    if value in (None, ""):
        return None
    t = spec["type"]
    if t in ("text", "longtext"):
        if not isinstance(value, str):
            raise ValueError(f"{name} must be text")
        value = value.strip() if t == "text" else value
        if len(value) > spec["max"]:
            raise ValueError(f"{name} is longer than {spec['max']} characters")
        return value
    if t == "email":
        value = str(value).strip().lower()
        if len(value) > 254 or not _EMAIL.match(value):
            raise ValueError(f"{name} must be an email address")
        return value
    if t == "url":
        value = str(value).strip()
        if len(value) > 2000 or not value.startswith(("https://", "http://")):
            raise ValueError(f"{name} must be an http(s) link")
        return value
    if t == "number":
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a number")
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if ("min" in spec and num < spec["min"]) or ("max" in spec and num > spec["max"]):
            raise ValueError(f"{name} is out of range")
        return int(num) if num.is_integer() else num
    if t == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false")
        return value
    if t == "enum":
        if value not in spec["options"] or value in (spec.get("hidden_options") or []):
            shown = [o for o in spec["options"] if o not in (spec.get("hidden_options") or [])]
            raise ValueError(f"{name} must be one of: {', '.join(shown)}")
        return value
    if t == "date":
        value = str(value)
        if not _DATE.match(value):
            raise ValueError(f"{name} must be a date (YYYY-MM-DD)")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{name} must be a real date") from None
        return value
    if t == "ref":
        try:
            ref = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a record id") from None
        if not AppRecord.objects.filter(installation=inst, space=space, collection=spec["collection"],
                                        pk=ref).exists():
            raise ValueError(f"{name}: no {spec['collection']} {ref}")
        return ref
    if t == "file":
        try:
            fid = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an uploaded file") from None
        f = AppFile.objects.filter(installation=inst, space=space, pk=fid).first()
        if f is None or f.claimed:
            raise ValueError(f"{name}: upload the file first")
        return fid
    raise ValueError(f"{name}: unknown type")  # pragma: no cover — the manifest validator forbids it


def _token(value, user, label: str, params: dict | None = None, rec_data: dict | None = None,
           rec_id: int | None = None):
    if not isinstance(value, str) or not value.startswith("$"):
        return value
    if value == "$actor.email":
        return getattr(user, "email", "") or label
    if value == "$actor.name":
        return (user.get_full_name() or user.email) if getattr(user, "is_authenticated", False) else label
    if value == "$now":
        return timezone.now().isoformat()
    if value == "$today":
        return timezone.localdate().isoformat()
    if value.startswith("$param."):
        return (params or {}).get(value.split(".", 1)[1])
    if value == "$record.id":
        return rec_id
    if value.startswith("$record."):
        return (rec_data or {}).get(value.split(".", 1)[1])
    return value


def _rate_limit(inst: AppInstallation, action: str, spec: dict, client_ip: str | None) -> None:
    """Counted from the event table, not a cache, so it holds across processes
    and restarts (the beta-request form's rule, apps/beta_requests)."""
    if not client_ip:
        return
    since = timezone.now() - timedelta(hours=1)
    n = AppRecordEvent.objects.filter(installation=inst, action=action, client_ip=client_ip, at__gte=since).count()
    if n >= spec["per_hour"]:
        raise TooManyRequests()


def setting(m: dict, key: str):
    s = (m.get("settings") or {}).get(key) or {}
    return s.get("value", s.get("default"))


def render_text(text: str, rec: AppRecord, m: dict, inst: AppInstallation, *, params: dict | None = None,
                link: str = "") -> str:
    """`{{field}}`, `{{ref_field.field}}` (one hop), `{{settings.key}}`,
    `{{param.name}}` (this send's own words) and `{{link}}` (the record's private link)."""
    fields = m["collections"][rec.collection]["fields"]

    def one(match):
        path = match.group(1).split(".")
        if path == ["link"]:
            return link
        if path[0] == "settings" and len(path) == 2:
            return str(setting(m, path[1]) or "")
        if path[0] == "param" and len(path) == 2:
            return str((params or {}).get(path[1], "") or "")
        if len(path) == 1:
            if fields.get(path[0], {}).get("read"):
                return ""  # a restricted field is never mailed
            return str(rec.data.get(path[0], "") or "")
        f = fields.get(path[0])
        if f and f["type"] == "ref" and rec.data.get(path[0]):
            other = AppRecord.objects.filter(installation=inst, space=rec.space, pk=rec.data[path[0]]).first()
            ofields = m["collections"].get(f["collection"], {}).get("fields", {})
            if ofields.get(path[1], {}).get("read"):
                return ""
            return str((other.data if other else {}).get(path[1], "") or "")
        return ""

    return _PLACEHOLDER.sub(one, text or "")


def _recipients(e: dict, rec: AppRecord, inst: AppInstallation, params: dict) -> list[str]:
    if e.get("to_param"):
        raw = params.get(e["to_param"]) or ""
    else:
        head, _, tail = e["to_field"].partition(".")
        if tail:
            other = AppRecord.objects.filter(installation=inst, space=rec.space, pk=rec.data.get(head) or 0).first()
            raw = (other.data if other else {}).get(tail) or ""
        else:
            raw = rec.data.get(head) or ""
    out = []
    for part in re.split(r"[,;\s]+", str(raw)):
        part = part.strip().lower()
        if part and _EMAIL.match(part) and part not in out:
            out.append(part)
    return out[:20]


def ics_invite(*, uid: str, title: str, day: str, at: str, minutes: int, location: str = "",
               description: str = "") -> str:
    """A minimal iCalendar request: every calendar (Google, Outlook, Apple)
    offers to add it, so an interview reaches calendars without a calendar API."""
    try:
        hh, mm = (int(x) for x in str(at or "09:00").split(":")[:2])
    except ValueError:
        hh, mm = 9, 0
    start = timezone.datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00")
    end = start + timedelta(minutes=minutes or 45)

    def esc(s: str) -> str:
        return str(s or "").replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")

    fmt = "%Y%m%dT%H%M%S"
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//canopy//workspace apps//EN", "METHOD:REQUEST",
        "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{timezone.now().strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{start.strftime(fmt)}", f"DTEND:{end.strftime(fmt)}", f"SUMMARY:{esc(title)}",
        f"LOCATION:{esc(location)}", f"DESCRIPTION:{esc(description)}", "END:VEVENT", "END:VCALENDAR", "",
    ])


def _effects(inst: AppInstallation, m: dict, spec: dict, rec: AppRecord, space: str,
             params: dict | None = None) -> list[dict]:
    """The email an action sends. Only ever to an address ON the record (or one
    ref away, or one the member typed for this send), worded by the app's
    settings or this send's params, and only from the live space on a server
    with app email switched on — a test copy and a scenario run never send."""
    params = params or {}
    done = []
    for e in spec.get("effects") or []:
        if e.get("enabled_setting") and not setting(m, e["enabled_setting"]):
            continue
        if not _matches(rec.data, e.get("when") or {}):
            continue
        to = _recipients(e, rec, inst, params)
        if not to:
            continue
        link = link_url(inst, mint_link(inst, e["link"], rec)) if e.get("link") else ""

        def word(k):
            if e.get(f"{k}_param") and params.get(e[f"{k}_param"]):
                return params[e[f"{k}_param"]]
            return setting(m, e.get(f"{k}_setting") or "") or ""

        subject = render_text(word("subject"), rec, m, inst, params=params, link=link).strip()[:200]
        body = render_text(word("body"), rec, m, inst, params=params, link=link)
        out = {"type": "email", "to": ", ".join(to), "subject": subject, "body": body}
        ics = None
        if e.get("ics") and rec.data.get(e["ics"]["date_field"]):
            dur = rec.data.get(e["ics"].get("duration_field") or "") or 45
            ics = ics_invite(uid=f"app-{inst.pk}-{rec.pk}@canopy", title=subject, day=rec.data[e["ics"]["date_field"]],
                             at=rec.data.get(e["ics"]["time_field"]) or "09:00",
                             minutes=int(dur) if str(dur).isdigit() else 45,
                             location=str(rec.data.get(e["ics"].get("location_field") or "") or ""), description=body)
            out["invite"] = True
        hours = min(float(params.get(e["delay_param"]) or 0), MAX_DELAY_HOURS) if e.get("delay_param") else 0
        if space != LIVE:
            out["status"] = "test copy — not sent"
        elif hours > 0:
            at = timezone.now() + timedelta(hours=hours)
            literal = {k: v for k, v in (spec.get("set") or {}).items() if not (isinstance(v, str) and v.startswith("$"))}
            AppScheduledEmail.objects.create(installation=inst, record=rec, action=str(spec.get("label") or "")[:40],
                                             to=out["to"], subject=subject, body=body,
                                             when={**(e.get("when") or {}), **literal}, send_at=at)
            out["status"] = f"scheduled for {at:%Y-%m-%d %H:%M} UTC"
        elif not getattr(django_settings, "WORKSPACE_APPS_SEND_EMAIL", False):
            out["status"] = "not sent — app email is off on this server"
        else:
            try:
                msg = EmailMessage(subject, body, django_settings.DEFAULT_FROM_EMAIL, to)
                if ics:
                    msg.attach("invite.ics", ics, "text/calendar; method=REQUEST")
                msg.send()
                out["status"] = "sent"
            except Exception as exc:  # pragma: no cover — SES outage
                out["status"] = f"failed: {exc}"[:200]
        done.append(out)
    return done


def _actor_named(rec: AppRecord, field: str, user, label: str) -> bool:
    who = (getattr(user, "email", "") or label or "").lower()
    named = [p.strip().lower() for p in re.split(r"[,;\s]+", str(rec.data.get(field) or "")) if p.strip()]
    return bool(who) and who in named


def _then(inst: AppInstallation, m: dict, action: str, spec: dict, rec: AppRecord, params: dict, user,
          label: str, space: str) -> None:
    """The steps the template declared after an action, written as the app."""
    for i, step in enumerate(spec.get("then") or []):
        if not _matches(rec.data, step.get("when") or {}):
            continue

        def val(v):
            return _token(v, user, label, params, rec.data, rec.pk)

        unless = step.get("unless")
        if unless:
            uq = AppRecord.objects.filter(installation=inst, space=space, collection=unless["collection"])
            for k, v in unless["where"].items():
                uq = uq.filter(**{f"data__{k}": val(v)})
            if uq.exists():
                continue
        tfields = m["collections"][step["collection"]]["fields"]
        values = {k: val(v) for k, v in step["set"].items()}
        values = {k: (_coerce(k, tfields[k], v, inst, space) if tfields[k]["type"] not in ("ref", "file") else v)
                  for k, v in values.items()}
        if step["op"] == "create":
            for k, f in tfields.items():
                if values.get(k) is None and "default" in f:
                    values[k] = f["default"]
            data = {k: v for k, v in values.items() if v is not None}
            target = AppRecord.objects.create(installation=inst, space=space, collection=step["collection"],
                                              data=data, created_by_label=label)
            changes = data
            if step.get("link_back"):
                rec.data = {**rec.data, step["link_back"]: target.pk}
                rec.save(update_fields=["data", "updated_at"])
        else:
            target_id = rec.pk if step["ref"] == "$record" else rec.data.get(step["ref"])
            target = (AppRecord.objects.select_for_update()
                      .filter(installation=inst, space=space, collection=step["collection"], pk=target_id or 0).first())
            if target is None or not _matches(target.data, step.get("if_target") or {}):
                continue
            changes = {k: [target.data.get(k), v] for k, v in values.items() if target.data.get(k) != v}
            if not changes:
                continue
            target.data = {k: v for k, v in {**target.data, **values}.items() if v is not None}
            target.save()
        AppRecordEvent.objects.create(
            installation=inst, space=space, record=target, record_ref=target.pk, collection=step["collection"],
            action=f"{action}.then[{i}]", op=step["op"], changes=changes, effects=[], actor_label=label,
        )


@transaction.atomic
def act(inst: AppInstallation, *, roles: set[str], action: str, record_id: int | None, input: dict,
        user, client_ip: str | None = None, public: bool = False, space: str = LIVE,
        manifest: dict | None = None, label: str = "", params: dict | None = None,
        link: dict | None = None) -> dict:
    """Run one declared action. Returns the written record (as its writer may
    see it: whole for a member, `{id}` alone for an anonymous visitor or a link
    holder). `link` is a verified link (`verify_link`): its holder runs only
    that link's actions, on that link's record alone."""
    m = manifest or manifest_for(inst, space)
    spec = m["actions"].get(action)
    if spec is None or not spec.get("enabled", True):
        raise Refused(f"no action '{action}'", status=404)
    if public and spec["role"] != "public":
        raise Refused(f"no action '{action}'", status=404)
    if link is not None:
        if spec["role"] != "link" or spec.get("link") != link["name"]:
            raise Refused(f"no action '{action}'", status=404)
    elif spec["role"] == "link":
        raise Refused(f"no action '{action}'", status=404)
    elif spec["role"] not in roles:
        raise Refused(f"'{action}' needs the '{spec['role']}' role")
    if not isinstance(input, dict):
        raise ValueError("input must be an object")
    fields = m["collections"][spec["collection"]]["fields"]
    unknown = sorted(set(input) - set(spec["input"]))
    if unknown:
        raise ValueError(f"'{action}' does not take: {', '.join(unknown)}")
    params_in = params or {}
    unknown = sorted(set(params_in) - set(spec.get("params") or {}))
    if unknown:
        raise ValueError(f"'{action}' takes no param: {', '.join(unknown)}")
    if spec["role"] in ("public", "link") and space == LIVE:
        _rate_limit(inst, action, spec, client_ip)

    authed = getattr(user, "is_authenticated", False)
    values = {k: _coerce(k, fields[k], input.get(k), inst, space) for k in spec["input"] if k in input}
    pvals = {k: _coerce(k, p, params_in.get(k), inst, space) for k, p in (spec.get("params") or {}).items()}
    missing = [p.get("label", k) for k, p in (spec.get("params") or {}).items() if p["required"] and pvals[k] is None]
    if missing:
        raise ValueError(f"required: {', '.join(missing)}")
    # A visitor with no account is labelled by the first address they gave (normalised).
    given = next((v for k, v in values.items() if fields[k]["type"] == "email" and v), "")
    label = (user.email if authed else "") or given or label or (f"link:{link['name']}" if link else "") or "anonymous"
    sc = scope_for(inst, m, space, user, roles) if not public and link is None else None
    for k, target in spec["refs"].items():
        if values.get(k) is not None:
            ref = AppRecord.objects.filter(installation=inst, space=space, pk=values[k]).first()
            if not _matches(ref.data, target["where"]):
                raise ValueError(f"{k}: that {fields[k]['collection']} is not available")
    for k, f in fields.items():
        if sc and f["type"] == "ref" and values.get(k) is not None and not sc.sees(f["collection"], values[k]):
            raise ValueError(f"{k}: that {f['collection']} is not available")
    values.update({k: _token(v, user, label, pvals) for k, v in spec["set"].items()})
    if link is not None and spec["op"] == "create":
        values[spec["link_ref"]] = link["record"]
    if link is not None and spec["op"] != "create":
        record_id = link["record"]

    if spec["op"] == "create":
        for k, f in fields.items():
            if values.get(k) is None and "default" in f:
                values[k] = f["default"]
        missing = [f.get("label", k) for k, f in fields.items() if f["required"] and values.get(k) is None]
        if missing:
            raise ValueError(f"required: {', '.join(missing)}")
        data = {k: v for k, v in values.items() if v is not None}
        rec = AppRecord.objects.create(installation=inst, space=space, collection=spec["collection"], data=data,
                                       created_by=user if authed else None, created_by_label=label)
        changes = data
    else:
        rec = (AppRecord.objects.select_for_update()
               .filter(installation=inst, space=space, collection=spec["collection"], pk=record_id or 0).first())
        if rec is None or (sc and not sc.sees(rec.collection, rec.pk)):
            raise Refused(f"no {spec['collection']} {record_id}", status=404)
        if spec.get("when") and not _matches(rec.data, spec["when"]):
            raise Refused(f"'{spec['label']}' does not apply to this {spec['collection']} as it stands", status=409)
        if spec.get("actor_field") and spec.get("override_role") not in roles \
                and not _actor_named(rec, spec["actor_field"], user, label):
            raise Refused(f"only the person named as {fields[spec['actor_field']].get('label', spec['actor_field'])} "
                          f"may {spec['label'].lower()}")
        if spec["op"] == "delete":
            changes = rec.data
        else:
            changes = {}
            for k, v in values.items():
                if rec.data.get(k) != v:
                    changes[k] = [rec.data.get(k), v]
            data = {**rec.data, **values}
            missing = [f.get("label", k) for k, f in fields.items() if f["required"] and data.get(k) is None]
            if missing:
                raise ValueError(f"required: {', '.join(missing)}")
            rec.data = {k: v for k, v in data.items() if v is not None}
            rec.save()
    file_ids = [v for k, v in values.items() if k in fields and fields[k]["type"] == "file" and v]
    if file_ids:
        AppFile.objects.filter(installation=inst, space=space, pk__in=file_ids).update(claimed=True)
    effects = [] if spec["op"] == "delete" else _effects(inst, m, spec, rec, space, pvals)
    AppRecordEvent.objects.create(
        installation=inst, space=space, record=None if spec["op"] == "delete" else rec, record_ref=rec.pk,
        collection=spec["collection"], action=action, op=spec["op"], changes=changes, effects=effects,
        actor=user if authed else None, actor_label=label, client_ip=client_ip,
    )
    if spec["op"] == "delete":
        rec.delete()
        return {"id": record_id}
    _then(inst, m, action, spec, rec, pvals, user, label, space)
    if public or link is not None:
        return {"id": rec.pk}
    rec.refresh_from_db()
    shown = readable(m["collections"][spec["collection"]], roles)
    return {**_row(rec, shown), "_effects": effects}


MAX_BULK = 200


def act_bulk(inst: AppInstallation, *, roles: set[str], action: str, record_ids: list[int], input: dict,
             params: dict | None, user, space: str = LIVE, manifest: dict | None = None) -> dict:
    """One action over many records (bulk reject): each runs alone, so one that
    is refused (already rejected) does not undo the others. Its emails render
    per record, so each candidate's own name and job are filled in."""
    m = manifest or manifest_for(inst, space)
    spec = m["actions"].get(action)
    if spec is None or not spec.get("bulk"):
        raise Refused(f"'{action}' cannot be run in bulk", status=404)
    ids = list(dict.fromkeys(int(i) for i in record_ids))
    if not ids or len(ids) > MAX_BULK:
        raise ValueError(f"name 1 to {MAX_BULK} records")
    done, failed = [], []
    for rid in ids:
        try:
            act(inst, roles=roles, action=action, record_id=rid, input=dict(input), params=params, user=user,
                space=space, manifest=m)
            done.append(rid)
        except (Refused, ValueError) as exc:
            failed.append({"id": rid, "error": str(exc)})
    return {"done": done, "failed": failed}


# --- private links (one record, one holder) ----------------------------------------------

_LINK_SALT = "workspace_apps.link"


def mint_link(inst: AppInstallation, name: str, rec: AppRecord) -> str:
    """A signed token naming one record of one installation for one link. It
    carries the record's space, so a test copy's link opens the test copy."""
    return signing.dumps({"i": inst.pk, "l": name, "r": rec.pk, "s": rec.space}, salt=_LINK_SALT, compress=True)


def link_url(inst: AppInstallation, token: str) -> str:
    base = getattr(django_settings, "CANOPY_PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}/w/{inst.workspace.slug}/app/{inst.key}/l/{token}"


def verify_link(inst: AppInstallation, token: str) -> dict:
    """{name, record, space, spec} for a valid, unexpired token of this
    installation whose record still exists — else Refused 404, the same for
    every reason so a guessed or stale link learns nothing."""
    try:
        raw = signing.loads(token, salt=_LINK_SALT, max_age=timedelta(days=mf.MAX_LINK_DAYS))
    except signing.BadSignature:
        raise Refused("this link is not valid", status=404) from None
    if not isinstance(raw, dict) or raw.get("i") != inst.pk:
        raise Refused("this link is not valid", status=404)
    m = manifest_for(inst, raw.get("s") or LIVE)
    spec = (m.get("links") or {}).get(raw.get("l"))
    if spec is None:
        raise Refused("this link is not valid", status=404)
    try:
        signing.loads(token, salt=_LINK_SALT, max_age=timedelta(days=spec["days"]))
    except signing.SignatureExpired:
        raise Refused("this link has expired", status=404) from None
    rec = AppRecord.objects.filter(installation=inst, space=raw.get("s") or LIVE, collection=spec["collection"],
                                   pk=raw.get("r")).first()
    if rec is None:
        raise Refused("this link is not valid", status=404)
    return {"name": raw["l"], "record": rec.pk, "space": rec.space, "spec": spec, "rec": rec, "manifest": m}


def link_view(inst: AppInstallation, link: dict) -> dict:
    """What a link's holder sees: the declared fields of its one record (one
    hop through a ref allowed), the link page, and the actions they may run."""
    m, spec, rec = link["manifest"], link["spec"], link["rec"]
    fields = m["collections"][spec["collection"]]["fields"]
    data: dict = {"id": rec.pk}
    for f in spec["fields"]:
        head, _, tail = f.partition(".")
        if not tail:
            data[head] = rec.data.get(head)
            continue
        other = AppRecord.objects.filter(installation=inst, space=rec.space, pk=rec.data.get(head) or 0).first()
        data.setdefault(head, {})
        if isinstance(data[head], dict):
            data[head][tail] = (other.data if other else {}).get(tail)
    page = next(p for p in m["pages"] if p["slug"] == spec["page"])
    acts = {a: {"label": m["actions"][a]["label"], "input": m["actions"][a]["input"],
                "op": m["actions"][a]["op"]} for a in spec["actions"]}
    shown = {f.partition(".")[0] for f in spec["fields"]} | {k for a in spec["actions"] for k in m["actions"][a]["input"]}
    data_model = {spec["collection"]: {"label": m["collections"][spec["collection"]]["label"],
                                       "fields": {k: v for k, v in fields.items() if k in shown}}}
    return {"link": link["name"], "label": spec["label"], "record": data, "page": page, "actions": acts,
            "data_model": data_model, "draft": rec.space != LIVE}


# --- feeds ---------------------------------------------------------------------------------


def feed(inst: AppInstallation, collection: str) -> list[dict]:
    """schema.org JobPosting entries for a public collection that declares a
    feed: built only from the public projection, so a feed shows nothing the
    public page could not."""
    m = manifest_for(inst, LIVE)
    spec = m["collections"].get(collection) or {}
    fd = (spec.get("public") or {}).get("feed")
    if fd is None:
        raise Refused("no feed", status=404)
    rows = query(inst, roles=set(), collection=collection, public=True, limit=MAX_QUERY_LIMIT, manifest=m)
    org = setting(m, fd["organization_setting"]) if fd.get("organization_setting") else ""
    org = org or inst.workspace.name
    base = getattr(django_settings, "CANOPY_PUBLIC_BASE_URL", "").rstrip("/")
    out = []
    for r in rows:
        g = {k: r.get(v) for k, v in fd["map"].items()}
        url = f"{base}/w/{inst.workspace.slug}/app/{inst.key}/{fd['page']}?id={r['id']}"
        entry = {"@context": "https://schema.org/", "@type": "JobPosting", "title": g.get("title") or "",
                 "description": g.get("description") or "", "datePosted": str(g.get("date_posted") or r["created_at"])[:10],
                 "hiringOrganization": {"@type": "Organization", "name": org}, "identifier": {
                     "@type": "PropertyValue", "name": org, "value": str(r["id"])}, "directApply": True, "url": url}
        if g.get("valid_through"):
            entry["validThrough"] = str(g["valid_through"])
        if g.get("employment_type"):
            entry["employmentType"] = str(g["employment_type"]).upper().replace("-", "_").replace(" ", "_")
        remote = g.get("remote")
        if remote in (True, "remote", "Remote"):
            entry["jobLocationType"] = "TELECOMMUTE"
            if g.get("country"):
                entry["applicantLocationRequirements"] = [{"@type": "Country", "name": c.strip()}
                                                          for c in str(g["country"]).split(",") if c.strip()]
        if g.get("location"):
            entry["jobLocation"] = {"@type": "Place", "address": {"@type": "PostalAddress",
                                                                  "addressLocality": str(g["location"]),
                                                                  **({"addressCountry": str(g["country"]).split(",")[0].strip()}
                                                                     if g.get("country") else {})}}
        if g.get("salary_min") is not None or g.get("salary_max") is not None:
            value = {"@type": "QuantitativeValue", "unitText": str(g.get("salary_unit") or "YEAR").upper()}
            if g.get("salary_min") is not None:
                value["minValue"] = g["salary_min"]
            if g.get("salary_max") is not None:
                value["maxValue"] = g["salary_max"]
            entry["baseSalary"] = {"@type": "MonetaryAmount", "currency": str(g.get("salary_currency") or "USD"),
                                   "value": value}
        if g.get("department"):
            entry["industry"] = str(g["department"])
        out.append(entry)
    return out


def feed_xml(inst: AppInstallation, collection: str) -> str:
    """The same entries as a plain XML feed (the shape job boards' "XML feed"
    importers take: one <job> per posting, apply URL, pay range)."""

    def tag(name, value):
        return f"<{name}>{escape(str(value))}</{name}>" if value not in (None, "") else ""

    jobs = []
    for e in feed(inst, collection):
        sal = (e.get("baseSalary") or {}).get("value") or {}
        loc = ((e.get("jobLocation") or {}).get("address") or {})
        jobs.append("<job>" + "".join([
            tag("partnerJobId", e["identifier"]["value"]), tag("title", e["title"]),
            tag("company", e["hiringOrganization"]["name"]), tag("applyUrl", e["url"]),
            tag("description", e["description"]), tag("location", loc.get("addressLocality")),
            tag("country", loc.get("addressCountry")), tag("workplaceTypes",
                                                           "Remote" if e.get("jobLocationType") else ""),
            tag("jobtype", e.get("employmentType")), tag("salaryMin", sal.get("minValue")),
            tag("salaryMax", sal.get("maxValue")), tag("salaryCurrency", (e.get("baseSalary") or {}).get("currency")),
            tag("listDate", e["datePosted"]),
        ]) + "</job>")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<source>' + "".join(jobs) + "</source>\n"


# --- retention -------------------------------------------------------------------------------

ANONYMIZED = "(removed)"


def _due(inst: AppInstallation, m: dict, rule: dict, now) -> list[AppRecord]:
    days = rule.get("after_days")
    if rule.get("after_setting"):
        try:
            days = int(setting(m, rule["after_setting"]) or 0)
        except (TypeError, ValueError):
            days = 0
    if not days or days < 1:
        return []  # a rule switched off (0 days) keeps everything
    cutoff = now - timedelta(days=days)
    qs = AppRecord.objects.filter(installation=inst, space=LIVE, collection=rule["collection"])
    for k, v in rule["where"].items():
        if isinstance(v, list):
            q = Q()
            for one in v:
                q |= Q(**{f"data__{k}": one})
            qs = qs.filter(q)
        else:
            qs = qs.filter(**{f"data__{k}": v})
    if rule.get("hold_field"):
        qs = qs.exclude(**{f"data__{rule['hold_field']}": True})
    df = rule["date_field"]
    if df in ("created_at", "updated_at"):
        qs = qs.filter(**{f"{df}__lt": cutoff})
        recs = list(qs[:5000])
    else:
        recs = [r for r in qs[:5000] if r.data.get(df) and str(r.data[df]) < cutoff.date().isoformat()]
    if rule.get("anonymize"):
        recs = [r for r in recs if r.data.get("_anonymized") != rule["name"]]
    return recs


def retention_preview(inst: AppInstallation, now=None) -> list[dict]:
    """Per rule: how many records would be anonymized or deleted now, and a few
    of them by title — what an admin looks at before the clock acts."""
    now = now or timezone.now()
    m = manifest_for(inst, LIVE)
    out = []
    for rule in m.get("retention") or []:
        recs = _due(inst, m, rule, now)
        title = m["collections"][rule["collection"]]["title_field"]
        out.append({"name": rule["name"], "label": rule["label"], "collection": rule["collection"],
                    "does": "delete" if rule.get("delete") else "anonymize", "count": len(recs),
                    "examples": [{"id": r.pk, "title": r.data.get(title)} for r in recs[:10]]})
    return out


@transaction.atomic
def retention_apply(inst: AppInstallation, now=None) -> dict:
    """Run the installation's retention rules on its live records. Each change
    is an event (`retention:<rule>`) naming fields, never their values."""
    now = now or timezone.now()
    m = manifest_for(inst, LIVE)
    counts = {}
    for rule in m.get("retention") or []:
        fields = m["collections"][rule["collection"]]["fields"]
        n = 0
        for rec in _due(inst, m, rule, now):
            for c in rule.get("cascade") or []:
                cc, _, cf = c.partition(".")
                kids = AppRecord.objects.filter(installation=inst, space=LIVE, collection=cc,
                                                **{f"data__{cf}": rec.pk})
                AppRecordEvent.objects.filter(record__in=kids).update(changes={})
                kids.delete()
            if rule.get("delete"):
                file_ids = [rec.data[k] for k, f in fields.items() if f["type"] == "file" and rec.data.get(k)]
                AppFile.objects.filter(installation=inst, pk__in=file_ids).delete()
                AppRecordEvent.objects.create(installation=inst, space=LIVE, record=None, record_ref=rec.pk,
                                              collection=rec.collection, action=f"retention:{rule['name']}",
                                              op="delete", changes={}, effects=[], actor_label="retention")
                rec.delete()
            else:
                data = dict(rec.data)
                file_ids = [data[k] for k in rule["anonymize"] if fields[k]["type"] == "file" and data.get(k)]
                AppFile.objects.filter(installation=inst, pk__in=file_ids).delete()
                for k in rule["anonymize"]:
                    if fields[k]["required"]:
                        data[k] = ANONYMIZED
                    else:
                        data.pop(k, None)
                data.update(rule.get("mark") or {})
                data["_anonymized"] = rule["name"]
                rec.data = data
                rec.save(update_fields=["data"])
                # The history kept the old values; it goes too, keeping that an event happened.
                AppRecordEvent.objects.filter(record=rec).update(changes={})
                AppRecordEvent.objects.create(installation=inst, space=LIVE, record=rec, record_ref=rec.pk,
                                              collection=rec.collection, action=f"retention:{rule['name']}",
                                              op="update", changes={k: "removed" for k in rule["anonymize"]},
                                              effects=[], actor_label="retention")
            n += 1
        counts[rule["name"]] = n
    return counts


def retention_sweep(now=None) -> dict:
    """Every enabled installation's rules (the off-request clock's door)."""
    out = {}
    for inst in AppInstallation.objects.filter(enabled=True).select_related("workspace"):
        if manifest_for(inst, LIVE).get("retention"):
            out[f"{inst.workspace.slug}/{inst.key}"] = retention_apply(inst, now=now)
    return out


def send_scheduled(now=None) -> dict:
    """Send every held-back email that is due (the clock's door). One whose record
    no longer matches what the action left it as (an un-rejected candidate, an
    anonymized address) is cancelled, not sent; each outcome lands in the
    record's history."""
    now = now or timezone.now()
    counts = {"sent": 0, "cancelled": 0, "failed": 0}
    due = (AppScheduledEmail.objects.filter(status=AppScheduledEmail.SCHEDULED, send_at__lte=now)
           .select_related("installation", "record"))
    for row in due[:500]:
        rec, inst = row.record, row.installation
        to = [a.strip() for a in row.to.split(",") if _EMAIL.match(a.strip())]
        if not inst.enabled or not _matches(rec.data, row.when) or not to:
            row.status, row.note = AppScheduledEmail.CANCELLED, "the record changed before it was due"
        elif not getattr(django_settings, "WORKSPACE_APPS_SEND_EMAIL", False):
            row.status, row.note = AppScheduledEmail.FAILED, "not sent — app email is off on this server"
        else:
            try:
                EmailMessage(row.subject, row.body, django_settings.DEFAULT_FROM_EMAIL, to).send()
                row.status, row.sent_at = AppScheduledEmail.SENT, now
            except Exception as exc:  # noqa: BLE001 — one failed send must not stop the rest
                row.status, row.note = AppScheduledEmail.FAILED, f"failed: {exc}"[:200]
        row.save(update_fields=["status", "note", "sent_at"])
        counts[row.status] += 1
        AppRecordEvent.objects.create(
            installation=inst, space=LIVE, record=rec, record_ref=rec.pk, collection=rec.collection,
            action=f"{row.action}"[:31] + " (email)", op="email", changes={},
            effects=[{"type": "email", "to": row.to, "subject": row.subject,
                      "status": row.status if not row.note else row.note}], actor_label="scheduled")
    return counts


def subject_export(inst: AppInstallation, *, email: str) -> dict:
    """Everything the app holds about one person, found by their address in any
    email field (a data-subject access request): records, their history and
    the names of their files. For an admin to send on — never public."""
    m = manifest_for(inst, LIVE)
    email = (email or "").strip().lower()
    if not _EMAIL.match(email):
        raise ValueError("an email address is required")
    found: dict[str, list] = {}
    ids = set()
    for cname, spec in m["collections"].items():
        efields = [k for k, f in spec["fields"].items() if f["type"] == "email"]
        q = Q()
        for k in efields:
            q |= Q(**{f"data__{k}": email})
        if not efields:
            continue
        for rec in AppRecord.objects.filter(installation=inst, space=LIVE, collection=cname).filter(q):
            ids.add(rec.pk)
            found.setdefault(cname, []).append(rec)
    # One hop: records that point at theirs (notes, interviews, scorecards, offers).
    for cname, spec in m["collections"].items():
        rfields = [k for k, f in spec["fields"].items() if f["type"] == "ref"]
        if not rfields or not ids:
            continue
        q = Q()
        for k in rfields:
            q |= Q(**{f"data__{k}__in": list(ids)})
        for rec in AppRecord.objects.filter(installation=inst, space=LIVE, collection=cname).filter(q):
            if rec not in found.get(cname, []):
                found.setdefault(cname, []).append(rec)
    out = {"email": email, "app": inst.key, "exported_at": timezone.now().isoformat(), "records": {}}
    for cname, recs in found.items():
        out["records"][cname] = [{**_row(r), "history": [
            {"action": e.action, "at": e.at.isoformat(), "by": e.actor_label} for e in r.events.all()]} for r in recs]
    return out


# --- files --------------------------------------------------------------------------------


def upload(inst: AppInstallation, *, space: str, name: str, content_type: str, body: bytes, label: str,
           client_ip: str | None, accept: list[str], max_mb: float) -> AppFile:
    name = attachment_storage.safe_filename(name or "file")
    ext = Path(name).suffix.lower()
    if ext not in accept:
        raise ValueError(f"only {', '.join(accept)} files")
    if len(body) > max_mb * 1024 * 1024:
        raise ValueError(f"the file is larger than {max_mb} MB")
    if space == LIVE and client_ip:
        since = timezone.now() - timedelta(hours=1)
        if AppFile.objects.filter(installation=inst, client_ip=client_ip, created_at__gte=since).count() >= 20:
            raise TooManyRequests()
    f = AppFile(installation=inst, space=space, name=name, content_type=content_type or "application/octet-stream",
                size=len(body), sha256=hashlib.sha256(body).hexdigest(), uploaded_by_label=label[:254],
                client_ip=client_ip)
    if attachment_storage.is_configured():
        f.save()
        f.storage_key = f"workspace-apps/{inst.workspace_id}/{inst.key}/{f.pk}/{name}"
        attachment_storage.put(f.storage_key, body, f.content_type)
        f.save(update_fields=["storage_key"])
    elif django_settings.DEBUG:
        f.local = body
        f.save()
    else:
        raise Refused("file uploads are not configured on this server", status=503)
    return f


def file_bytes(f: AppFile) -> bytes:
    if f.storage_key:
        return attachment_storage.get(f.storage_key).body
    return bytes(f.local or b"")


def readable_file(inst: AppInstallation, *, roles: set[str], file_id: int, space: str, user=None) -> AppFile:
    """A file the caller may read: one a record they can read points at."""
    m = manifest_for(inst, space)
    f = AppFile.objects.filter(installation=inst, space=space, pk=file_id).first()
    if f is None:
        raise Refused("not found", status=404)
    sc = scope_for(inst, m, space, user, roles)
    for cname, c in m["collections"].items():
        if not may_read(c, roles):
            continue
        ids = sc.allowed(cname) if sc else None
        for fname, fs in c["fields"].items():
            if fs["type"] != "file":
                continue
            qs = AppRecord.objects.filter(installation=inst, space=space, collection=cname, **{f"data__{fname}": file_id})
            if ids is not None:
                qs = qs.filter(pk__in=ids)
            if qs.exists():
                return f
    raise Refused("not found", status=404)
