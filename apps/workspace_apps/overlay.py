"""The overlay: a workspace's changes to an app, kept apart from the template.

What runs is `compose(template manifest, overlay)`. The overlay is a list of
keyed operations, each naming the thing it changes by a stable key and carrying
the plain-language request (`intent`) that produced it. Because the template is
never copied, a template update is the same overlay composed over the newer
version — the research's one finding that held across Shopify, Salesforce, SAP,
Power Platform and Figma: customizations that are DATA over declared anchors
survive upgrades; customizations that edit the vendor's artifact do not.

The rules that keep composition safe:

* The workspace ADDS in its own namespace (`x_` fields, collections, actions,
  roles; `x-` pages), so a later template addition can never collide with it.
* It may change a template element only through a narrow door: relabel, help
  text, hide, require (never un-require), add options to a list the template
  marked extensible (between its fixed first and last), switch an action off,
  add its own fields to an action's input, fill a slot the page declares, set a
  setting. Anything else is a fork (`fork_page`) and is shown as one.
* Ops apply one at a time and the result is validated after each. An op whose
  anchor no longer exists, or that the template has since made invalid, is
  QUARANTINED with its reason — never silently dropped, never allowed to break
  the app (Power Platform's "Conflicts" tab, not Odoo's failed view).
"""
from __future__ import annotations

import copy
import uuid

from django.utils import timezone

from . import manifest as mf

PREFIX = "x_"
PAGE_PREFIX = "x-"

OP_TYPES = ("add_field", "set_field", "add_option", "set_option", "add_collection", "add_action", "set_action",
            "add_public_field", "add_role", "set_setting", "fill_slot", "add_page", "fork_page", "set_page",
            "add_scenario")

#: What a person must look at twice: a change that alters what the public sees or
#: can submit, or who can do what. Publishing one needs a workspace admin.
RISK_REASONS = {
    "add_public_field": "shows a field on the public pages",
    "add_role": "adds a role (who can do what)",
}


class OpError(ValueError):
    pass


def new_op(raw: dict, *, by: str = "", intent: str = "") -> dict:
    """Normalise one incoming op: stamp an id, who, when and why."""
    if not isinstance(raw, dict) or raw.get("op") not in OP_TYPES:
        raise OpError(f"op must be one of: {', '.join(OP_TYPES)}")
    op = {k: v for k, v in raw.items() if k not in ("id", "by", "at")}
    op["id"] = uuid.uuid4().hex[:12]
    op["intent"] = str(raw.get("intent") or intent or "")[:1000]
    op["by"] = by[:254]
    op["at"] = timezone.now().isoformat()
    return op


def _need(op: dict, *keys: str):
    missing = [k for k in keys if op.get(k) in (None, "")]
    if missing:
        raise OpError(f"{op['op']} needs {', '.join(missing)}")


def _collection(m: dict, name: str) -> dict:
    c = m["collections"].get(name)
    if c is None:
        raise OpError(f"the app has no collection '{name}'")
    return c


def _field(m: dict, coll: str, name: str) -> dict:
    f = _collection(m, coll)["fields"].get(name)
    if f is None:
        raise OpError(f"'{coll}' has no field '{name}'")
    return f


def _page(m: dict, slug: str) -> dict:
    p = next((p for p in m["pages"] if p["slug"] == slug), None)
    if p is None:
        raise OpError(f"the app has no page '{slug}'")
    return p


def _own(name: str, what: str, prefix: str = PREFIX):
    if not isinstance(name, str) or not name.startswith(prefix):
        raise OpError(f"a workspace's own {what} is named '{prefix}…' so the template can never collide with it")


def _apply(m: dict, op: dict) -> None:
    """Apply one op to `m` in place, or raise OpError naming why it cannot apply."""
    kind = op["op"]
    if kind == "add_field":
        _need(op, "collection", "key", "field")
        _own(op["key"], "field")
        coll = _collection(m, op["collection"])
        if op["key"] in coll["fields"]:
            raise OpError(f"'{op['collection']}' already has a field '{op['key']}'")
        coll["fields"][op["key"]] = {**op["field"], "origin": "workspace"}
    elif kind == "set_field":
        _need(op, "collection", "field")
        f = _field(m, op["collection"], op["field"])
        ours = f.get("origin") == "workspace"
        for k in ("label", "help"):
            if op.get(k) is not None:
                f[k] = str(op[k])
        if "hidden" in op:
            if op["hidden"] and f.get("required"):
                raise OpError(f"'{op['field']}' is required, so it cannot be hidden")
            f["hidden"] = bool(op["hidden"])
        if "required" in op:
            if not op["required"] and f.get("required") and not ours:
                raise OpError("a template field's 'required' cannot be removed — only added")
            f["required"] = bool(op["required"])
    elif kind == "add_option":
        _need(op, "collection", "field", "option")
        f = _field(m, op["collection"], op["field"])
        if f["type"] != "enum" or not f.get("extensible"):
            raise OpError(f"the template does not let workspaces add options to '{op['field']}'")
        if op["option"] in f["options"]:
            return  # the template now has it: nothing to do
        opts = f["options"]
        at = len(opts)
        if op.get("before") in opts:
            at = opts.index(op["before"])
        elif f.get("fixed_last") in opts:
            at = opts.index(f["fixed_last"])
        if f.get("fixed_first") in opts:
            at = max(at, opts.index(f["fixed_first"]) + 1)
        opts.insert(at, op["option"])
    elif kind == "set_option":
        _need(op, "collection", "field", "option")
        f = _field(m, op["collection"], op["field"])
        if f["type"] != "enum" or op["option"] not in f["options"]:
            raise OpError(f"'{op['field']}' has no option '{op['option']}'")
        if op.get("label"):
            f.setdefault("option_labels", {})[op["option"]] = str(op["label"])
        if op.get("hidden") is not None:
            if op["hidden"] and op["option"] in (f.get("fixed_first"), f.get("fixed_last"), f.get("default")):
                raise OpError(f"'{op['option']}' is fixed by the template and cannot be hidden")
            hidden = set(f.get("hidden_options") or [])
            (hidden.add if op["hidden"] else hidden.discard)(op["option"])
            f["hidden_options"] = [o for o in f["options"] if o in hidden]
    elif kind == "add_collection":
        _need(op, "key", "collection")
        _own(op["key"], "collection")
        if op["key"] in m["collections"]:
            raise OpError(f"there is already a collection '{op['key']}'")
        spec = copy.deepcopy(op["collection"])
        spec["origin"] = "workspace"
        spec.pop("public", None)  # a workspace collection goes public only through add_public_field
        m["collections"][op["key"]] = spec
    elif kind == "add_action":
        _need(op, "key", "action")
        _own(op["key"], "action")
        if op["key"] in m["actions"]:
            raise OpError(f"there is already an action '{op['key']}'")
        spec = op["action"] if isinstance(op["action"], dict) else {}
        # `then` writes records as the app, and a link action answers an anonymous
        # holder: both are the template's to declare, never a workspace change.
        if spec.get("then"):
            raise OpError("a workspace action cannot declare 'then' steps — only the template can")
        if spec.get("role") == "link":
            raise OpError("a workspace action cannot be a link action — only the template can")
        m["actions"][op["key"]] = {**op["action"], "origin": "workspace"}
    elif kind == "set_action":
        _need(op, "action")
        a = m["actions"].get(op["action"])
        if a is None:
            raise OpError(f"the app has no action '{op['action']}'")
        if "enabled" in op:
            a["enabled"] = bool(op["enabled"])
        if op.get("label"):
            a["label"] = str(op["label"])
        for f in op.get("add_input") or []:
            if not str(f).startswith(PREFIX):
                raise OpError("a workspace may add only its own (x_) fields to a template action's input")
            if f not in m["collections"][a["collection"]]["fields"]:
                raise OpError(f"'{a['collection']}' has no field '{f}'")
            if f not in a["input"]:
                a["input"].append(f)
    elif kind == "add_public_field":
        _need(op, "collection", "field")
        _own(op["field"], "public field")
        coll = _collection(m, op["collection"])
        _field(m, op["collection"], op["field"])
        if "public" not in coll:
            raise OpError(f"'{op['collection']}' has no public projection to add to")
        if op["field"] not in coll["public"]["fields"]:
            coll["public"]["fields"].append(op["field"])
    elif kind == "add_role":
        _need(op, "key")
        _own(op["key"], "role")
        if op["key"] in m["roles"]:
            raise OpError(f"there is already a role '{op['key']}'")
        m["roles"][op["key"]] = {"capability": op.get("capability"), "description": op.get("description", ""),
                                 "origin": "workspace"}
    elif kind == "set_setting":
        _need(op, "key")
        s = m["settings"].get(op["key"])
        if s is None:
            raise OpError(f"the app has no setting '{op['key']}'")
        s["value"] = op.get("value")
    elif kind == "fill_slot":
        _need(op, "page", "slot", "code")
        p = _page(m, op["page"])
        if op["slot"] not in p.get("slots", []):
            raise OpError(f"page '{op['page']}' has no slot '{op['slot']}'")
        key = op.get("key") or op["id"]
        fills = p.setdefault("fills", {}).setdefault(op["slot"], [])
        fills[:] = [f for f in fills if f["key"] != key] + [{"key": key, "code": op["code"]}]
    elif kind == "add_page":
        _need(op, "page")
        spec = copy.deepcopy(op["page"])
        _own(spec.get("slug"), "page", PAGE_PREFIX)
        if any(p["slug"] == spec["slug"] for p in m["pages"]):
            raise OpError(f"there is already a page '{spec['slug']}'")
        spec["origin"] = "workspace"
        m["pages"].append(spec)
    elif kind == "fork_page":
        _need(op, "page", "code")
        p = _page(m, op["page"])
        p["code"], p["forked"] = op["code"], True
    elif kind == "set_page":
        _need(op, "page")
        p = _page(m, op["page"])
        if op.get("title"):
            p["title"] = str(op["title"])
        if "nav" in op:
            p["nav"] = bool(op["nav"])
    elif kind == "add_scenario":
        _need(op, "scenario")
        m["scenarios"].append({**op["scenario"], "origin": "workspace"})


def compose(template_manifest: dict, overlay: list) -> tuple[dict, list]:
    """(the manifest that runs, the quarantined ops [{op, reason}])."""
    m = copy.deepcopy(template_manifest)
    bad = [k for c in m["collections"].values() for k in c["fields"] if k.startswith(PREFIX)]
    if bad:  # pragma: no cover — templates are validated at release
        raise ValueError(f"a template may not use the workspace namespace: {bad}")
    quarantine = []
    for op in overlay or []:
        candidate = copy.deepcopy(m)
        try:
            _apply(candidate, op)
            candidate = mf.validate(candidate)
        except (OpError, ValueError, KeyError, TypeError) as exc:
            quarantine.append({"op": op, "reason": str(exc)})
            continue
        m = candidate
    return m, quarantine


def check(template_manifest: dict, overlay: list, op: dict) -> dict:
    """Compose with `op` added; raise OpError if `op` itself would be quarantined."""
    m, q = compose(template_manifest, [*overlay, op])
    mine = next((x for x in q if x["op"]["id"] == op["id"]), None)
    if mine:
        raise OpError(mine["reason"])
    return m


def is_risky(op: dict, effective: dict) -> str | None:
    """Why `op` needs an admin's eye before it goes live, or None."""
    kind = op["op"]
    if kind in RISK_REASONS:
        return RISK_REASONS[kind]
    action = effective.get("actions", {}).get(op.get("action") or op.get("key") or "")
    if kind == "set_action" and action and action["role"] == "public" and op.get("add_input"):
        return "changes what the public form accepts"
    if kind == "add_action" and (op.get("action") or {}).get("role") == "public":
        return "adds a form the public can submit"
    page = next((p for p in effective.get("pages", []) if p["slug"] == (op.get("page") if isinstance(op.get("page"), str)
                                                                        else (op.get("page") or {}).get("slug"))), None)
    if kind in ("fill_slot", "fork_page", "add_page") and page and page["audience"] == "public":
        return "changes a public page"
    return None


def _label(effective: dict, coll: str, field: str | None = None) -> str:
    c = effective.get("collections", {}).get(coll, {})
    cl = c.get("label") or coll
    if field is None:
        return cl
    f = c.get("fields", {}).get(field, {})
    return f"“{f.get('label') or field}” on {cl}"


def describe(op: dict, effective: dict) -> str:
    """One plain-English line for a person reviewing the change."""
    k = op["op"]
    if k == "add_field":
        f = op.get("field") or {}
        return f"Adds a {f.get('type', '')} field “{f.get('label') or op.get('key')}” to {_label(effective, op.get('collection', ''))}."
    if k == "set_field":
        bits = [f"label “{op['label']}”" if op.get("label") else "", "help text" if op.get("help") else "",
                ("hidden" if op.get("hidden") else "shown") if "hidden" in op else "",
                ("required" if op.get("required") else "optional") if "required" in op else ""]
        return f"Changes {_label(effective, op.get('collection', ''), op.get('field'))}: {', '.join(b for b in bits if b)}."
    if k == "add_option":
        return f"Adds “{op.get('option')}” to {_label(effective, op.get('collection', ''), op.get('field'))}."
    if k == "set_option":
        what = f"renames it “{op['label']}”" if op.get("label") else ("hides it" if op.get("hidden") else "shows it")
        return f"Option “{op.get('option')}” of {_label(effective, op.get('collection', ''), op.get('field'))}: {what}."
    if k == "add_collection":
        return f"Adds a new kind of record, “{(op.get('collection') or {}).get('label') or op.get('key')}”."
    if k == "add_action":
        return f"Adds an action “{(op.get('action') or {}).get('label') or op.get('key')}”."
    if k == "set_action":
        a = effective.get("actions", {}).get(op.get("action", ""), {})
        bits = []
        if "enabled" in op:
            bits.append("switched on" if op["enabled"] else "switched off")
        if op.get("add_input"):
            bits.append("now also asks for " + ", ".join(_label(effective, a.get("collection", ""), f)
                                                         for f in op["add_input"]))
        return f"“{a.get('label') or op.get('action')}”: {'; '.join(bits) or 'relabelled'}."
    if k == "add_public_field":
        return f"Shows {_label(effective, op.get('collection', ''), op.get('field'))} on the public pages."
    if k == "add_role":
        return f"Adds a role “{op.get('key')}”."
    if k == "set_setting":
        s = effective.get("settings", {}).get(op.get("key", ""), {})
        return f"Sets “{s.get('label') or op.get('key')}”."
    if k == "fill_slot":
        return f"Adds to the “{op.get('slot')}” area of the {op.get('page')} page."
    if k == "add_page":
        return f"Adds a page “{(op.get('page') or {}).get('title') or (op.get('page') or {}).get('slug')}”."
    if k == "fork_page":
        return f"Replaces the {op.get('page')} page with the workspace's own — it will no longer receive template updates."
    if k == "set_page":
        return f"Changes the {op.get('page')} page's title or menu entry."
    if k == "add_scenario":
        return f"Adds a check: “{(op.get('scenario') or {}).get('name')}”."
    return k  # pragma: no cover
