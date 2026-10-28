"""The app manifest: the whole of what an app is, and the one validator for it.

An app declares what it is, and can do nothing it did not declare:

* ``roles`` — who may do what, each derived from a WORKSPACE capability
  (`apps/workspaces/permissions.py`), never from a role name, or given only by
  grant. Built in: ``member`` (anyone in the workspace) and ``public`` (anyone,
  on the app's public pages only).
* ``collections`` — typed records. Each names the role that may ``read`` it and,
  optionally, a ``public`` projection: the ``where`` every anonymous read is
  narrowed to and the only ``fields`` it returns.
* ``actions`` — the ONLY writes: create, update or delete one record of one
  collection, from declared ``input`` fields, by one role, with optional
  ``effects`` the platform performs (an email). A ``public`` action is a form an
  anonymous visitor submits; it only creates and is rate-limited per address.
* ``pages`` — dynamic-view code drawn in the sandbox, for an ``audience``, with
  named ``slots`` a workspace may fill without forking the page.
* ``settings`` — the knobs a workspace sets without code (careers intro, email
  wording). ``fixtures`` seed a draft's test copy; ``scenarios`` are the
  plain-language checks a version must pass before it is published.
* ``links`` — a private page for ONE record, opened from a signed link the app
  emails (a candidate picking an interview time, answering an offer). The
  holder reads that record's declared fields and runs only the ``link`` actions
  named, on that record alone.
* ``retention`` — rules the platform enforces on a clock: records matching a
  ``where`` and untouched for N days have fields blanked (anonymized) or are
  deleted, so personal data does not outlive its purpose.

An action may also guard on the record's state (``when``), on who the record
names (``actor_field``: only the approver approves), take ``params`` that are
used but never stored (the wording of one email), and declare ``then`` steps —
records the platform writes after it (a stage-change log row; a job opening once
no approval is pending). A field may be readable by one role only (``read``): a
candidate's voluntary demographic answers, an offer's salary.

A template's manifest is the base; a workspace's changes are an overlay on it
(`overlay.py`), and what runs is the two composed — validated here again, so a
composition that breaks a rule is refused like a hand-written one.
"""
from __future__ import annotations

import re
from typing import Any

from apps.dynamic_views import catalog as dv_catalog
from apps.dynamic_views import services as dv_services
from apps.workspaces import permissions as perms

IDENT = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
SLUG = re.compile(r"^[a-z][a-z0-9-]{0,38}[a-z0-9]$|^[a-z]$")
KEY = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")
_RAW_FORM = re.compile(r"<form[\s>]")
_MODAL = re.compile(r"\b(?:window\.)?(?:confirm|alert|prompt)\s*\(\s*[`'\"]")
#: canopy's own page of every app (`/w/<ws>/apps/<key>/customize`).
RESERVED_SLUGS = ("customize",)
_DEFINES_FILL = re.compile(r"\bfunction\s+Fill\s*\(|\b(?:const|let|var)\s+Fill\s*=")

BUILTIN_ROLES = ("member", "public", "link")
FIELD_TYPES = ("text", "longtext", "email", "url", "number", "bool", "enum", "date", "ref", "file")
SETTING_TYPES = ("text", "longtext", "bool", "color", "email", "number")
OPS = ("create", "update", "delete")
AUDIENCES = ("member", "public", "link")
#: Values an action's ``set`` may use instead of a constant.
SET_TOKENS = ("$actor.email", "$actor.name", "$now", "$today")
#: A `then` step's values may also name the acting record or a param.
_THEN_VALUE = re.compile(r"^\$(record\.(id|[a-z][a-z0-9_]{0,39})|param\.[a-z][a-z0-9_]{0,39})$")
PARAM_TYPES = ("text", "longtext", "email", "date", "enum", "bool", "number")
EFFECT_TYPES = ("email",)

MAX_COLLECTIONS = 24
MAX_FIELDS = 60
MAX_ACTIONS = 80
MAX_PAGES = 30
MAX_THEN = 5
MAX_LINK_DAYS = 60
MAX_FILE_MB = 10
DEFAULT_PUBLIC_PER_HOUR = 10


def _fail(where: str, why: str):
    raise ValueError(f"{where}: {why}")


def check_page_code(where: str, code: str, *, fill: bool = False) -> str:
    """Page (`function View`) or slot-fill (`function Fill`) code: the sandbox's rules."""
    if fill:
        code = code or ""
        if not code.strip() or len(code.encode()) > dv_services.MAX_CODE_BYTES:
            _fail(where, "a slot fill is JSX defining `function Fill(props)`, under 200 kB")
        if not _DEFINES_FILL.search(code):
            _fail(where, "a slot fill must define `function Fill(props)`")
    else:
        try:
            code = dv_services.validate_code(code)
        except ValueError as exc:
            _fail(where, str(exc))
    if _MODAL.search(code):
        _fail(where, "use the kit's <Button confirm=\"…\"> (or show the message on the page), not "
                     "confirm()/alert()/prompt(): the sandbox has no allow-modals, so confirm() returns false "
                     "without asking and the button never acts")
    if _RAW_FORM.search(code):
        _fail(where, "use the kit's <Form onSubmit>, not <form>: the sandbox has no allow-forms, "
                     "so a <form> never submits — its button would silently do nothing")
    return code


def _validate_roles(raw) -> dict:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        _fail("roles", "must be an object of {name: {capability, description?}}")
    out = {}
    for name, spec in raw.items():
        if not IDENT.match(name) or name in BUILTIN_ROLES:
            _fail(f"roles.{name}", "must be an identifier, and not 'member' or 'public'")
        if not isinstance(spec, dict):
            _fail(f"roles.{name}", "must be an object")
        cap = spec.get("capability")
        if cap is not None and cap not in perms.MINIMUM_ROLE:
            _fail(f"roles.{name}.capability", f"must be one of: {', '.join(sorted(perms.MINIMUM_ROLE))}, "
                                                "or omitted for a role given only by grant")
        out[name] = {"capability": cap, "description": str(spec.get("description") or "")[:300],
                     "origin": spec.get("origin", "template")}
    return out


def _validate_field(where: str, spec, collections: set[str], roles: set[str] | None = None) -> dict:
    if not isinstance(spec, dict):
        _fail(where, "must be an object with a type")
    ftype = spec.get("type")
    if ftype not in FIELD_TYPES:
        _fail(f"{where}.type", f"must be one of: {', '.join(FIELD_TYPES)}")
    out: dict[str, Any] = {"type": ftype, "required": bool(spec.get("required", False)),
                           "origin": spec.get("origin", "template")}
    for text in ("label", "help"):
        if spec.get(text):
            out[text] = str(spec[text])[:300]
    if spec.get("hidden"):
        out["hidden"] = True
    if spec.get("read") is not None:
        # Only this role reads the field; everyone else gets the record without it.
        if roles is not None and (spec["read"] not in roles or spec["read"] in BUILTIN_ROLES):
            _fail(f"{where}.read", "must name a role of this app (not member, public or link)")
        out["read"] = spec["read"]
    if ftype in ("text", "longtext"):
        default_max = 500 if ftype == "text" else 20_000
        mx = spec.get("max", default_max)
        if not isinstance(mx, int) or not 1 <= mx <= 50_000:
            _fail(f"{where}.max", "must be an integer from 1 to 50000")
        out["max"] = mx
    if ftype == "number":
        for bound in ("min", "max"):
            if bound in spec:
                if not isinstance(spec[bound], (int, float)) or isinstance(spec[bound], bool):
                    _fail(f"{where}.{bound}", "must be a number")
                out[bound] = spec[bound]
    if ftype == "enum":
        options = spec.get("options")
        if not isinstance(options, list) or not options or not all(isinstance(o, str) and o for o in options):
            _fail(f"{where}.options", "must be a non-empty list of strings")
        if len(set(options)) != len(options):
            _fail(f"{where}.options", "are not unique")
        out["options"] = list(options)
        #: A workspace may add options between `fixed_first` and `fixed_last`.
        out["extensible"] = bool(spec.get("extensible", False))
        for end in ("fixed_first", "fixed_last"):
            if spec.get(end):
                if spec[end] not in options:
                    _fail(f"{where}.{end}", "must be one of the options")
                out[end] = spec[end]
        hidden = spec.get("hidden_options") or []
        if not isinstance(hidden, list) or any(h not in options for h in hidden):
            _fail(f"{where}.hidden_options", "must list options")
        if hidden:
            out["hidden_options"] = list(hidden)
        labels = spec.get("option_labels") or {}
        if not isinstance(labels, dict) or any(k not in options for k in labels):
            _fail(f"{where}.option_labels", "must map options to labels")
        if labels:
            out["option_labels"] = {k: str(v)[:120] for k, v in labels.items()}
    if ftype == "ref":
        target = spec.get("collection")
        if target not in collections:
            _fail(f"{where}.collection", "must name a collection of this app")
        out["collection"] = target
    if ftype == "file":
        accept = spec.get("accept") or [".pdf", ".doc", ".docx"]
        if not isinstance(accept, list) or not all(isinstance(a, str) and a.startswith(".") for a in accept):
            _fail(f"{where}.accept", "must be a list of extensions like '.pdf'")
        out["accept"] = [a.lower() for a in accept]
        mb = spec.get("max_mb", 5)
        if not isinstance(mb, (int, float)) or not 0 < mb <= MAX_FILE_MB:
            _fail(f"{where}.max_mb", f"must be at most {MAX_FILE_MB}")
        out["max_mb"] = mb
    if "default" in spec:
        out["default"] = spec["default"]
    return out


def _validate_where(where: str, raw, fields: dict, *, lists: bool = False) -> dict:
    """{field: value}; with `lists`, a value may also be a list (any of)."""
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        _fail(where, "must be an object of {field: value}")
    for k, v in raw.items():
        if k not in fields:
            _fail(f"{where}.{k}", "is not a field of the collection")
        vals = v if (lists and isinstance(v, list)) else [v]
        if not vals or any(not isinstance(x, (str, int, float, bool)) or x is None for x in vals):
            _fail(f"{where}.{k}", "must be a string, number or boolean" + (" (or a list of them)" if lists else ""))
    return dict(raw)


def _validate_collections(raw, roles: set[str]) -> dict:
    if not isinstance(raw, dict) or not raw:
        _fail("collections", "must be a non-empty object of {name: {fields, read, public?}}")
    if len(raw) > MAX_COLLECTIONS:
        _fail("collections", f"at most {MAX_COLLECTIONS}")
    names = set(raw)
    out = {}
    for name, spec in raw.items():
        where = f"collections.{name}"
        if not IDENT.match(name):
            _fail(where, "name must be an identifier")
        if not isinstance(spec, dict):
            _fail(where, "must be an object")
        fields_raw = spec.get("fields")
        if not isinstance(fields_raw, dict) or not fields_raw:
            _fail(f"{where}.fields", "must be a non-empty object")
        if len(fields_raw) > MAX_FIELDS:
            _fail(f"{where}.fields", f"at most {MAX_FIELDS}")
        fields = {}
        for fname, fspec in fields_raw.items():
            if not IDENT.match(fname) or fname in ("id", "created_at", "updated_at"):
                _fail(f"{where}.fields.{fname}", "must be an identifier, and not id/created_at/updated_at")
            fields[fname] = _validate_field(f"{where}.fields.{fname}", fspec, names, roles)
        read = spec.get("read", "member")
        if read not in roles or read == "public":
            _fail(f"{where}.read", "must name a role of this app (not 'public' — use the public projection)")
        entry = {"fields": fields, "read": read, "title_field": spec.get("title_field") or next(iter(fields)),
                 "label": str(spec.get("label") or name)[:120], "origin": spec.get("origin", "template")}
        if entry["title_field"] not in fields:
            _fail(f"{where}.title_field", "must be a field")
        public = spec.get("public")
        if public is not None:
            if not isinstance(public, dict):
                _fail(f"{where}.public", "must be an object of {where?, fields}")
            pfields = public.get("fields")
            if not isinstance(pfields, list) or not pfields or any(f not in fields for f in pfields):
                _fail(f"{where}.public.fields", "must list fields of the collection")
            if any(fields[f]["type"] == "file" for f in pfields):
                _fail(f"{where}.public.fields", "a file is never public")
            if any(fields[f].get("read") for f in pfields):
                _fail(f"{where}.public.fields", "a field only one role may read is never public")
            entry["public"] = {"where": _validate_where(f"{where}.public.where", public.get("where"), fields),
                               "fields": list(pfields)}
            if public.get("feed") is not None:
                entry["public"]["feed"] = _validate_feed(f"{where}.public.feed", public["feed"], pfields)
        out[name] = entry
    return out


def _validate_effect(where: str, raw, fields: dict, settings: dict, params: dict, links: dict) -> dict:
    """An email the platform sends after the action: to an address ON the record
    (or one hop through a ref, or a param), worded by settings or by params the
    person wrote for this one send, optionally carrying a calendar invite and a
    private link."""
    if not isinstance(raw, dict) or raw.get("type") not in EFFECT_TYPES:
        _fail(where, f"must be an object with type one of: {', '.join(EFFECT_TYPES)}")
    out: dict[str, Any] = {"type": "email"}
    if raw.get("to_param") is not None:
        if raw["to_param"] not in params or params[raw["to_param"]]["type"] not in ("email", "text"):
            _fail(f"{where}.to_param", "must name an email (or text list of emails) param of the action")
        out["to_param"] = raw["to_param"]
    else:
        to = raw.get("to_field")
        head, _, tail = str(to or "").partition(".")
        if tail:
            if head not in fields or fields[head]["type"] != "ref":
                _fail(f"{where}.to_field", "'ref.field' must start with a ref field of the collection")
        elif to not in fields or fields[to]["type"] not in ("email", "text"):
            _fail(f"{where}.to_field", "must name an email field of the collection (or 'ref.email')")
        out["to_field"] = to
    for k in ("subject", "body"):
        s, p = raw.get(f"{k}_setting"), raw.get(f"{k}_param")
        if p is not None:
            if p not in params or params[p]["type"] not in ("text", "longtext"):
                _fail(f"{where}.{k}_param", "must name a text param of the action")
            out[f"{k}_param"] = p
        if s is not None or p is None:
            if s not in settings or settings[s]["type"] not in ("text", "longtext"):
                _fail(f"{where}.{k}_setting", "must name a text setting of the app")
            out[f"{k}_setting"] = s
    gate = raw.get("enabled_setting")
    if gate is not None:
        if gate not in settings or settings[gate]["type"] != "bool":
            _fail(f"{where}.enabled_setting", "must name a bool setting of the app")
        out["enabled_setting"] = gate
    when = raw.get("when")
    if when is not None:
        out["when"] = _validate_where(f"{where}.when", when, fields)
    ics = raw.get("ics")
    if ics is not None:
        if not isinstance(ics, dict):
            _fail(f"{where}.ics", "must be {date_field, time_field, duration_field?, title_setting?}")
        for k in ("date_field", "time_field"):
            if ics.get(k) not in fields:
                _fail(f"{where}.ics.{k}", "must name a field of the collection")
        if fields[ics["date_field"]]["type"] != "date":
            _fail(f"{where}.ics.date_field", "must be a date field")
        if ics.get("duration_field") is not None and ics["duration_field"] not in fields:
            _fail(f"{where}.ics.duration_field", "must name a field of the collection")
        out["ics"] = {k: ics[k] for k in ("date_field", "time_field", "duration_field", "location_field")
                      if ics.get(k)}
    if raw.get("link") is not None:
        if raw["link"] not in links:
            _fail(f"{where}.link", "must name a link of this app")
        out["link"] = raw["link"]
    return out


def _validate_params(where: str, raw) -> dict:
    """Values an action takes but never stores: they word one email, or feed `then`."""
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict) or len(raw) > 10:
        _fail(where, "must be an object of at most 10 {name: {type, label?, required?}}")
    out = {}
    for k, spec in raw.items():
        if not IDENT.match(k) or not isinstance(spec, dict) or spec.get("type") not in PARAM_TYPES:
            _fail(f"{where}.{k}", f"must be an identifier naming {{type}} of: {', '.join(PARAM_TYPES)}")
        out[k] = _validate_field(f"{where}.{k}", spec, set())
    return out


def _validate_then(where: str, raw, coll: str, collections: dict, params: dict) -> list:
    """Records the platform writes after the action, as the app (not the actor):
    `create` a record of a collection, or `update` the record a ref field points
    at — `unless` some record still matches (no approval left pending)."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_THEN:
        _fail(where, f"must be a list of at most {MAX_THEN}")
    fields = collections[coll]["fields"]
    out = []
    for i, step in enumerate(raw):
        sw = f"{where}[{i}]"
        if not isinstance(step, dict) or step.get("op") not in ("create", "update"):
            _fail(sw, "must be {op: create, collection, set} or {op: update, ref, set, unless?}")
        if step["op"] == "create":
            target = step.get("collection")
            if target not in collections:
                _fail(f"{sw}.collection", "must name a collection of this app")
            entry = {"op": "create", "collection": target}
        else:
            ref = step.get("ref")
            if ref == "$record":
                target = coll
            elif ref not in fields or fields[ref]["type"] != "ref":
                _fail(f"{sw}.ref", "must name a ref field of the action's collection, or '$record'")
            else:
                target = fields[ref]["collection"]
            entry = {"op": "update", "ref": ref, "collection": target}
        tfields = collections[target]["fields"]
        sets = step.get("set")
        if not isinstance(sets, dict) or not sets:
            _fail(f"{sw}.set", "must be a non-empty object of {field: value}")
        for k, v in sets.items():
            if k not in tfields:
                _fail(f"{sw}.set.{k}", f"is not a field of '{target}'")
            if isinstance(v, str) and v.startswith("$"):
                m = _THEN_VALUE.match(v)
                if v in SET_TOKENS:
                    continue
                if not m:
                    _fail(f"{sw}.set.{k}", f"tokens are {', '.join(SET_TOKENS)}, $record.<field>, $param.<name>")
                name = v.split(".", 1)[1]
                if v.startswith("$record.") and name != "id" and name not in fields:
                    _fail(f"{sw}.set.{k}", f"'{coll}' has no field '{name}'")
                if v.startswith("$param.") and name not in params:
                    _fail(f"{sw}.set.{k}", f"the action has no param '{name}'")
        entry["set"] = dict(sets)
        if step.get("when") is not None:
            entry["when"] = _validate_where(f"{sw}.when", step["when"], fields, lists=True)
        if step.get("if_target") is not None:
            if step["op"] != "update":
                _fail(f"{sw}.if_target", "guards the record an update step changes")
            entry["if_target"] = _validate_where(f"{sw}.if_target", step["if_target"], tfields, lists=True)
        unless = step.get("unless")
        if unless is not None:
            if not isinstance(unless, dict) or unless.get("collection") not in collections:
                _fail(f"{sw}.unless", "must be {collection, where} naming a collection")
            ufields = collections[unless["collection"]]["fields"]
            uwhere = unless.get("where") or {}
            if not isinstance(uwhere, dict) or any(k not in ufields for k in uwhere):
                _fail(f"{sw}.unless.where", "must use fields of that collection")
            for k, v in uwhere.items():
                if isinstance(v, str) and v.startswith("$") and not _THEN_VALUE.match(v):
                    _fail(f"{sw}.unless.where.{k}", "may name $record.<field> or $param.<name>")
            entry["unless"] = {"collection": unless["collection"], "where": dict(uwhere)}
        out.append(entry)
    return out


def _validate_actions(raw, roles: set[str], collections: dict, settings: dict, links: dict) -> dict:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        _fail("actions", "must be an object of {name: {role, op, collection, input?, set?, refs?, effects?}}")
    if len(raw) > MAX_ACTIONS:
        _fail("actions", f"at most {MAX_ACTIONS}")
    out = {}
    for name, spec in raw.items():
        where = f"actions.{name}"
        if not IDENT.match(name):
            _fail(where, "name must be an identifier")
        if not isinstance(spec, dict):
            _fail(where, "must be an object")
        role = spec.get("role")
        if role not in roles:
            _fail(f"{where}.role", "must name a role of this app")
        op = spec.get("op")
        if op not in OPS:
            _fail(f"{where}.op", f"must be one of: {', '.join(OPS)}")
        coll = spec.get("collection")
        if coll not in collections:
            _fail(f"{where}.collection", "must name a collection of this app")
        fields = collections[coll]["fields"]
        if role == "public" and op != "create":
            _fail(f"{where}.op", "a public action may only create: an anonymous visitor changes nothing that exists")
        if role == "link":
            link = links.get(spec.get("link"))
            if link is None:
                _fail(f"{where}.link", "a link action names the link (of this app) whose holder runs it")
            if name not in link["actions"]:
                _fail(f"{where}.link", f"link '{spec['link']}' does not list this action")
            if op == "update" and coll != link["collection"]:
                _fail(f"{where}.collection", "a link action updates only the link's own record")
            if op == "create":
                lr = spec.get("link_ref")
                if lr not in fields or fields[lr]["type"] != "ref" or fields[lr]["collection"] != link["collection"]:
                    _fail(f"{where}.link_ref", "a link action creates only a record pointing (link_ref) at the link's record")
            if op == "delete":
                _fail(f"{where}.op", "a link action never deletes")
        inputs = spec.get("input") or []
        if not isinstance(inputs, list) or any(f not in fields for f in inputs):
            _fail(f"{where}.input", "must list fields of the collection")
        if op == "delete" and inputs:
            _fail(f"{where}.input", "a delete takes no input")
        params = _validate_params(f"{where}.params", spec.get("params"))
        if set(params) & set(inputs):
            _fail(f"{where}.params", "a param is not also an input")
        sets = spec.get("set") or {}
        if not isinstance(sets, dict):
            _fail(f"{where}.set", "must be an object of {field: value}")
        for k, v in sets.items():
            if k not in fields:
                _fail(f"{where}.set.{k}", "is not a field of the collection")
            if k in inputs:
                _fail(f"{where}.set.{k}", "is also an input: a set field is the action's, not the caller's")
            if isinstance(v, str) and v.startswith("$") and v not in SET_TOKENS:
                if not (v.startswith("$param.") and v.split(".", 1)[1] in params):
                    _fail(f"{where}.set.{k}", f"tokens are {', '.join(SET_TOKENS)} or $param.<name>")
        refs = spec.get("refs") or {}
        if not isinstance(refs, dict):
            _fail(f"{where}.refs", "must be an object of {ref_field: {where}}")
        norm_refs = {}
        for k, r in refs.items():
            if k not in inputs or fields[k]["type"] != "ref":
                _fail(f"{where}.refs.{k}", "must be a ref field the action takes as input")
            target = collections[fields[k]["collection"]]["fields"]
            norm_refs[k] = {"where": _validate_where(f"{where}.refs.{k}.where", (r or {}).get("where"), target)}
        if role == "public":
            for k in inputs:
                if fields[k]["type"] == "ref" and k not in norm_refs:
                    _fail(f"{where}.refs.{k}", "a public action must say which records a ref may point at")
        when = spec.get("when")
        if when is not None and op == "create":
            _fail(f"{where}.when", "guards the existing record, so only an update or delete takes one")
        actor_field = spec.get("actor_field")
        if actor_field is not None:
            if op == "create" or actor_field not in fields or fields[actor_field]["type"] not in ("email", "text"):
                _fail(f"{where}.actor_field", "must name an email field of the record (update/delete only)")
        effects_raw = spec.get("effects") or []
        if not isinstance(effects_raw, list) or len(effects_raw) > 5:
            _fail(f"{where}.effects", "must be a list of at most 5")
        effects = [_validate_effect(f"{where}.effects[{i}]", e, fields, settings, params, links)
                   for i, e in enumerate(effects_raw)]
        if role in ("public", "link") and any("enabled_setting" not in e for e in effects):
            _fail(f"{where}.effects", "a public or link action's email must be switchable by a bool setting "
                                      "(enabled_setting)")
        if role in ("public", "link") and any("to_param" in e for e in effects):
            _fail(f"{where}.effects", "a public or link action emails only an address on the record")
        entry = {"role": role, "op": op, "collection": coll, "input": list(inputs), "set": dict(sets),
                 "refs": norm_refs, "label": str(spec.get("label") or name)[:120], "effects": effects,
                 "enabled": bool(spec.get("enabled", True)), "origin": spec.get("origin", "template"),
                 "params": params, "then": _validate_then(f"{where}.then", spec.get("then"), coll, collections,
                                                          params),
                 "bulk": bool(spec.get("bulk", False)) and op != "create" and role not in ("public", "link")}
        if when is not None:
            entry["when"] = _validate_where(f"{where}.when", when, fields, lists=True)
        if actor_field is not None:
            entry["actor_field"] = actor_field
            # A role that may act on any record regardless (an admin standing in for an approver).
            ov = spec.get("override_role")
            if ov is not None and ov not in roles:
                _fail(f"{where}.override_role", "must name a role of this app")
            if ov:
                entry["override_role"] = ov
        if role == "link":
            entry["link"] = spec["link"]
            if op == "create":
                entry["link_ref"] = spec["link_ref"]
        if role in ("public", "link"):
            per_hour = spec.get("per_hour", DEFAULT_PUBLIC_PER_HOUR)
            if not isinstance(per_hour, int) or not 1 <= per_hour <= 1000:
                _fail(f"{where}.per_hour", "must be an integer from 1 to 1000")
            entry["per_hour"] = per_hour
        out[name] = entry
    return out


#: What a feed entry may carry: schema.org JobPosting's words, mapped to public fields.
FEED_TYPES = ("JobPosting",)
FEED_KEYS = ("title", "description", "location", "employment_type", "date_posted", "valid_through",
             "salary_min", "salary_max", "salary_currency", "salary_unit", "remote", "country", "department")


def _validate_feed(where: str, raw, pfields: list) -> dict:
    """A public collection's machine-readable feed (Google for Jobs JSON-LD, an
    RSS/XML feed job boards pull), built only from public fields."""
    if not isinstance(raw, dict) or raw.get("type") not in FEED_TYPES:
        _fail(where, f"must be {{type: one of {', '.join(FEED_TYPES)}, map, page}}")
    fmap = raw.get("map")
    if not isinstance(fmap, dict) or "title" not in fmap:
        _fail(f"{where}.map", "must map at least 'title' to a public field")
    for k, v in fmap.items():
        if k not in FEED_KEYS:
            _fail(f"{where}.map.{k}", f"must be one of: {', '.join(FEED_KEYS)}")
        if v not in (*pfields, "created_at", "updated_at"):
            _fail(f"{where}.map.{k}", "must name a public field (or created_at/updated_at)")
    page = raw.get("page")
    if not isinstance(page, str) or not SLUG.match(page):
        _fail(f"{where}.page", "must name the public page that shows one entry (opened as ?id=<id>)")
    return {"type": raw["type"], "map": dict(fmap), "page": page,
            "organization_setting": str(raw.get("organization_setting") or "")}


def _validate_links(raw, collections: dict) -> dict:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict) or len(raw) > 10:
        _fail("links", "must be an object of at most 10 {name: {collection, fields, actions, days?}}")
    out = {}
    for name, spec in raw.items():
        where = f"links.{name}"
        if not IDENT.match(name) or not isinstance(spec, dict):
            _fail(where, "must be an identifier naming an object")
        coll = spec.get("collection")
        if coll not in collections:
            _fail(f"{where}.collection", "must name a collection of this app")
        fields = collections[coll]["fields"]
        lfields = spec.get("fields")
        if not isinstance(lfields, list) or not lfields:
            _fail(f"{where}.fields", "must list the fields the link's holder may read")
        for f in lfields:
            head, _, tail = f.partition(".")
            if head not in fields:
                _fail(f"{where}.fields", f"'{f}' is not a field of '{coll}'")
            if fields[head].get("read"):
                _fail(f"{where}.fields", f"'{head}' is readable by one role only")
            if tail:  # one hop through a ref: the job's title on an interview link
                if fields[head]["type"] != "ref":
                    _fail(f"{where}.fields", f"'{head}' is not a ref")
                tf = collections[fields[head]["collection"]]["fields"]
                if tail not in tf or tf[tail].get("read") or tf[tail]["type"] == "file":
                    _fail(f"{where}.fields", f"'{f}' cannot be read through a link")
        acts = spec.get("actions") or []
        if not isinstance(acts, list) or not all(isinstance(a, str) for a in acts):
            _fail(f"{where}.actions", "must list action names")
        days = spec.get("days", 30)
        if not isinstance(days, int) or not 1 <= days <= MAX_LINK_DAYS:
            _fail(f"{where}.days", f"must be 1 to {MAX_LINK_DAYS}")
        page = spec.get("page")
        if not isinstance(page, str) or not SLUG.match(page):
            _fail(f"{where}.page", "must name the link page (audience 'link') the holder opens")
        out[name] = {"collection": coll, "fields": list(lfields), "actions": list(acts), "days": days, "page": page,
                     "label": str(spec.get("label") or name)[:120]}
    return out


def _validate_retention(raw, collections: dict, settings: dict) -> list:
    """Rules the platform runs on a clock. Each rule's `where` is ANDed with an
    age: records whose `date_field` (default updated_at) is older than
    `after_days`, or the number in setting `after_setting`, have `anonymize`
    fields blanked, or are deleted with their files."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > 20:
        _fail("retention", "must be a list of at most 20 rules")
    out = []
    for i, r in enumerate(raw):
        where = f"retention[{i}]"
        if not isinstance(r, dict) or not IDENT.match(str(r.get("name") or "")):
            _fail(where, "needs a name (an identifier)")
        coll = r.get("collection")
        if coll not in collections:
            _fail(f"{where}.collection", "must name a collection of this app")
        fields = collections[coll]["fields"]
        entry: dict[str, Any] = {"name": r["name"], "collection": coll, "label": str(r.get("label") or r["name"])[:200],
                                 "where": _validate_where(f"{where}.where", r.get("where"), fields, lists=True)}
        if r.get("after_setting") is not None:
            s = settings.get(r["after_setting"])
            if s is None or s["type"] != "number":
                _fail(f"{where}.after_setting", "must name a number setting (days)")
            entry["after_setting"] = r["after_setting"]
        else:
            days = r.get("after_days")
            if not isinstance(days, int) or not 1 <= days <= 3650:
                _fail(f"{where}.after_days", "must be 1 to 3650 days (or use after_setting)")
            entry["after_days"] = days
        df = r.get("date_field", "updated_at")
        if df not in ("created_at", "updated_at") and (df not in fields or fields[df]["type"] != "date"):
            _fail(f"{where}.date_field", "must be created_at, updated_at or a date field")
        entry["date_field"] = df
        hold = r.get("hold_field")
        if hold is not None:
            # A legal hold: records with this bool field true are never touched.
            if hold not in fields or fields[hold]["type"] != "bool":
                _fail(f"{where}.hold_field", "must name a bool field of the collection")
            entry["hold_field"] = hold
        if r.get("delete"):
            entry["delete"] = True
        else:
            anon = r.get("anonymize")
            if not isinstance(anon, list) or not anon or any(f not in fields for f in anon):
                _fail(f"{where}.anonymize", "must list fields to blank (or set delete: true)")
            if any(fields[f]["required"] and fields[f]["type"] not in ("text", "longtext", "email", "url")
                   for f in anon):
                _fail(f"{where}.anonymize", "a required field that is not text cannot be blanked")
            entry["anonymize"] = list(anon)
        cascade = r.get("cascade") or []
        if not isinstance(cascade, list):
            _fail(f"{where}.cascade", "must list 'collection.ref_field' whose records go with it")
        for c in cascade:
            cc, _, cf = str(c).partition(".")
            cfield = (collections.get(cc) or {}).get("fields", {}).get(cf)
            if not cfield or cfield["type"] != "ref" or cfield["collection"] != coll:
                _fail(f"{where}.cascade", f"'{c}' must be a ref field pointing at '{coll}'")
        if cascade:
            # Records that point at it (notes, scorecards) hold its person's words: deleted with it.
            entry["cascade"] = list(cascade)
            marks = r.get("mark") or {}
            if not isinstance(marks, dict) or any(k not in fields for k in marks):
                _fail(f"{where}.mark", "must set fields of the collection")
            entry["mark"] = dict(marks)
        out.append(entry)
    return out


def _validate_source(where: str, raw, audience: str, collections: dict) -> dict:
    if not isinstance(raw, dict):
        _fail(where, "must be an object")
    alias = str(raw.get("alias") or "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,39}", alias):
        _fail(f"{where}.alias", "must be an identifier")
    if "collection" in raw:
        coll = raw["collection"]
        if coll not in collections:
            _fail(f"{where}.collection", "must name a collection of this app")
        if audience == "public" and "public" not in collections[coll]:
            _fail(f"{where}.collection", f"'{coll}' has no public projection, so a public page cannot read it")
        entry = {"alias": alias, "collection": coll}
        fields = collections[coll]["fields"]
        if raw.get("where"):
            entry["where"] = _validate_where(f"{where}.where", raw["where"], fields, lists=True)
        if raw.get("order"):
            order = str(raw["order"])
            if order.lstrip("-") not in (*fields, "created_at", "updated_at"):
                _fail(f"{where}.order", "must be a field, created_at or updated_at, optionally prefixed '-'")
            entry["order"] = order
        if raw.get("limit") is not None:
            if not isinstance(raw["limit"], int) or not 1 <= raw["limit"] <= 2000:
                _fail(f"{where}.limit", "must be an integer from 1 to 2000")
            entry["limit"] = raw["limit"]
        return entry
    if audience == "public":
        _fail(where, "a public page reads only the app's own collections")
    # A canopy catalog read: the dynamic-view rule, applied by the dynamic-view validator.
    return dv_catalog.validate_sources([raw])[0]


def _validate_pages(raw, roles: set[str], collections: dict, links: dict | None = None) -> list:
    if not isinstance(raw, list) or not raw:
        _fail("pages", "must be a non-empty list")
    if len(raw) > MAX_PAGES:
        _fail("pages", f"at most {MAX_PAGES}")
    out, seen = [], set()
    for i, spec in enumerate(raw):
        where = f"pages[{i}]"
        if not isinstance(spec, dict):
            _fail(where, "must be an object")
        slug = spec.get("slug")
        if not isinstance(slug, str) or not SLUG.match(slug):
            _fail(f"{where}.slug", "must be lowercase letters, digits and hyphens")
        if slug in seen:
            _fail(f"{where}.slug", f"'{slug}' is used twice")
        if slug in RESERVED_SLUGS:
            _fail(f"{where}.slug", f"'{slug}' is canopy's own page of every app")
        seen.add(slug)
        audience = spec.get("audience", "member")
        if audience not in AUDIENCES:
            _fail(f"{where}.audience", "must be 'member', 'public' or 'link'")
        role = spec.get("role", audience if audience in ("public", "link") else "member")
        if audience == "link":
            if role != "link":
                _fail(f"{where}.role", "a link page's role is 'link'")
        elif role not in roles or role == "link" or (audience == "member") == (role == "public"):
            _fail(f"{where}.role", "a member page names a role of this app; a public page's role is 'public'")
        code = check_page_code(f"{where}.code", spec.get("code") or "")
        sources_raw = spec.get("sources") or []
        if audience == "link" and sources_raw:
            _fail(f"{where}.sources", "a link page reads only its link's record")
        if not isinstance(sources_raw, list) or len(sources_raw) > 20:
            _fail(f"{where}.sources", "must be a list of at most 20")
        sources = [_validate_source(f"{where}.sources[{j}]", s, audience, collections)
                   for j, s in enumerate(sources_raw)]
        if len({s["alias"] for s in sources}) != len(sources):
            _fail(f"{where}.sources", "aliases must be unique")
        slots = spec.get("slots") or []
        if not isinstance(slots, list) or any(not isinstance(s, str) or not IDENT.match(s) for s in slots):
            _fail(f"{where}.slots", "must be a list of identifiers")
        fills = spec.get("fills") or {}
        if not isinstance(fills, dict) or any(s not in slots for s in fills):
            _fail(f"{where}.fills", "may only fill declared slots")
        norm_fills = {s: [{"key": f["key"], "code": check_page_code(f"{where}.fills.{s}", f.get("code"), fill=True)}
                          for f in fl] for s, fl in fills.items()}
        out.append({"slug": slug, "title": str(spec.get("title") or slug)[:120], "audience": audience,
                    "role": role, "nav": bool(spec.get("nav", True)), "code": code, "sources": sources,
                    "slots": list(slots), "fills": norm_fills, "origin": spec.get("origin", "template"),
                    "forked": bool(spec.get("forked", False))})
    return out


def _validate_settings(raw) -> dict:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        _fail("settings", "must be an object of {key: {type, label, default?}}")
    out = {}
    for key, spec in raw.items():
        where = f"settings.{key}"
        if not IDENT.match(key) or not isinstance(spec, dict):
            _fail(where, "must be an identifier naming an object")
        t = spec.get("type")
        if t not in SETTING_TYPES:
            _fail(f"{where}.type", f"must be one of: {', '.join(SETTING_TYPES)}")
        value = spec.get("value", spec.get("default"))
        out[key] = {"type": t, "label": str(spec.get("label") or key)[:120], "help": str(spec.get("help") or "")[:300],
                    "default": spec.get("default"), "value": coerce_setting(where, t, value)}
    return out


def coerce_setting(where: str, t: str, value):
    if value is None:
        return None
    if t == "bool":
        if not isinstance(value, bool):
            _fail(where, "must be true or false")
        return value
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100_000:
            _fail(where, "must be a number from 0 to 100000")
        return value
    if not isinstance(value, str):
        _fail(where, "must be text")
    if t == "color" and not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        _fail(where, "must be a colour like #1f6feb")
    if len(value) > (20_000 if t == "longtext" else 500):
        _fail(where, "is too long")
    return value


def _validate_fixtures(raw, collections: dict) -> dict:
    """Sample records a draft's test copy is seeded with. A ref names another
    fixture by its `_as` alias ("@job_engineer")."""
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict) or any(c not in collections for c in raw):
        _fail("fixtures", "must be an object of {collection: [records]}")
    for c, rows in raw.items():
        if not isinstance(rows, list) or len(rows) > 200 or not all(isinstance(r, dict) for r in rows):
            _fail(f"fixtures.{c}", "must be a list of at most 200 objects")
    return raw


def _validate_scenarios(raw, roles: set[str], actions: dict, collections: dict) -> list:
    """Plain-language checks run against a scratch copy before publishing:
    `steps` are actions run as a role, `expect` counts what should then exist."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > 50:
        _fail("scenarios", "must be a list of at most 50")
    out = []
    for i, sc in enumerate(raw):
        where = f"scenarios[{i}]"
        if not isinstance(sc, dict) or not str(sc.get("name") or "").strip():
            _fail(where, "needs a name")
        steps = sc.get("steps")
        if not isinstance(steps, list) or not steps:
            _fail(f"{where}.steps", "must be a non-empty list")
        for j, st in enumerate(steps):
            sw = f"{where}.steps[{j}]"
            if not isinstance(st, dict):
                _fail(sw, "must be an object")
            if "act" in st:
                if st["act"] not in actions:
                    _fail(f"{sw}.act", "must name an action of this app")
                if st.get("as", actions[st["act"]]["role"]) not in roles:
                    _fail(f"{sw}.as", "must name a role of this app")
            elif "expect" in st:
                ex = st["expect"]
                if not isinstance(ex, dict) or ex.get("collection") not in collections:
                    _fail(f"{sw}.expect", "must name a collection")
            else:
                _fail(sw, "is an {act, as?, input, record?, save?} or an {expect: {collection, where?, count}}")
        out.append({"name": str(sc["name"])[:200], "steps": steps, "origin": sc.get("origin", "template")})
    return out


def validate(raw) -> dict:
    """A normalised copy of `raw`, or ValueError naming what is wrong."""
    if not isinstance(raw, dict):
        raise ValueError("manifest must be an object")
    key = raw.get("key")
    if not isinstance(key, str) or not KEY.match(key):
        _fail("key", "must be 3-40 lowercase letters, digits and hyphens, starting with a letter")
    name = str(raw.get("name") or "").strip()
    if not name:
        _fail("name", "is required")
    roles = _validate_roles(raw.get("roles"))
    names = set(roles) | set(BUILTIN_ROLES)
    settings = _validate_settings(raw.get("settings"))
    collections = _validate_collections(raw.get("collections"), names)
    links = _validate_links(raw.get("links"), collections)
    actions = _validate_actions(raw.get("actions"), names, collections, settings, links)
    for lname, link in links.items():
        for a in link["actions"]:
            if a not in actions or actions[a]["role"] != "link" or actions[a].get("link") != lname:
                _fail(f"links.{lname}.actions", f"'{a}' must be an action of role 'link' naming this link")
    pages = _validate_pages(raw.get("pages"), names, collections, links)
    for lname, link in links.items():
        if not any(p["slug"] == link["page"] and p["audience"] == "link" for p in pages):
            _fail(f"links.{lname}.page", f"'{link['page']}' must be a page with audience 'link'")
    for cname, c in collections.items():
        feed = (c.get("public") or {}).get("feed")
        if feed and not any(p["slug"] == feed["page"] and p["audience"] == "public" for p in pages):
            _fail(f"collections.{cname}.public.feed.page", f"'{feed['page']}' must be a public page")
    return {
        "key": key, "name": name[:120], "description": str(raw.get("description") or "")[:2000],
        "version": str(raw.get("version") or "")[:40], "changelog": str(raw.get("changelog") or "")[:5000],
        "roles": roles, "settings": settings, "collections": collections, "actions": actions, "pages": pages,
        "links": links, "retention": _validate_retention(raw.get("retention"), collections, settings),
        "fixtures": _validate_fixtures(raw.get("fixtures"), collections),
        "scenarios": _validate_scenarios(raw.get("scenarios"), names, actions, collections),
    }
