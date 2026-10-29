"""What a TEMPLATE update changes about who can see and do what — in plain words.

A workspace's own changes are judged one op at a time (`overlay.is_risky`). A
template update is not an op: it swaps the whole template version under the
workspace's changes, so it is judged here, by comparing the two versions on
the surfaces where a mistake exposes data or hands out power:

* roles — added, removed, or held by a different workspace capability;
* reading — a collection's `read`/`read_also`, a field's `read`, a `scope`;
* the public — public and private-link pages, public projections, public and
  link actions (what a visitor can submit), private links;
* acting — an action's role;
* email — who an email goes to;
* retention — what is anonymized or deleted, and when.

Each finding is a sentence a non-developer can act on. Any finding means a
workspace admin publishes the update (`services.publish`), exactly as for a
risky change of the workspace's own. Nothing here names an app.
"""
from __future__ import annotations

OUTSIDE = ("public", "link")

#: A workspace capability, as the people who hold it (the store says it the same way).
HOLDERS = {"content.write": "every editor", "members.manage": "workspace admins", "integrations": "workspace admins",
           "read": "every member"}


def _role_text(spec: dict | None) -> str:
    cap = (spec or {}).get("capability")
    return HOLDERS.get(cap, f"everyone with the '{cap}' permission") if cap else "only people given it by name"


def _recipients(action: dict) -> list:
    return sorted(str(e.get("to_field") or e.get("to_param") or "") for e in action.get("effects") or [])


def _page_key(p: dict) -> tuple:
    return (p["audience"], p.get("role"), p.get("code"), repr(p.get("sources")))


def changes(old: dict, new: dict) -> list[str]:
    """Plain-language findings, empty when the update changes none of the surfaces above."""
    out: list[str] = []
    # Roles.
    ro, rn = old.get("roles") or {}, new.get("roles") or {}
    for r in sorted(set(rn) - set(ro)):
        out.append(f"adds the role '{r}', held by {_role_text(rn[r])}")
    for r in sorted(set(ro) - set(rn)):
        out.append(f"removes the role '{r}'")
    for r in sorted(set(ro) & set(rn)):
        if ro[r].get("capability") != rn[r].get("capability"):
            out.append(f"changes who holds '{r}': from {_role_text(ro[r])} to {_role_text(rn[r])}")
    # Reading.
    co, cn = old.get("collections") or {}, new.get("collections") or {}
    scoped = []
    for c in sorted(set(cn) & set(co)):
        a, b = co[c], cn[c]
        label = b.get("label") or c
        if a.get("read") != b.get("read") or sorted(a.get("read_also") or []) != sorted(b.get("read_also") or []):
            out.append(f"changes who reads {label}: {_readers(a)} → {_readers(b)}")
        if a.get("scope") != b.get("scope"):
            scoped.append(label)
        fo, fn = a.get("fields") or {}, b.get("fields") or {}
        for f in sorted(set(fo) & set(fn)):
            if fo[f].get("read") != fn[f].get("read"):
                out.append(f"changes who reads “{fn[f].get('label') or f}” on {label}: "
                           f"{fo[f].get('read') or 'whoever reads the record'} → {fn[f].get('read') or 'whoever reads the record'}")
        po, pn = a.get("public") or {}, b.get("public") or {}
        added = sorted(set(pn.get("fields") or []) - set(po.get("fields") or []))
        if added:
            out.append(f"shows more of {label} publicly: {', '.join(added)}")
        if po.get("where") != pn.get("where") and pn:
            out.append(f"changes which {label} records are public")
    if scoped:
        out.append(f"changes which records each person sees (per-record access) in: {', '.join(scoped)}")
    for c in sorted(set(cn) - set(co)):
        if cn[c].get("public"):
            out.append(f"adds {cn[c].get('label') or c}, shown publicly")
    # Pages outside the workspace, and who reaches member pages.
    po = {p["slug"]: p for p in old.get("pages") or []}
    for p in new.get("pages") or []:
        before = po.get(p["slug"])
        outside = p["audience"] in OUTSIDE or (before and before["audience"] in OUTSIDE)
        if before is None:
            if p["audience"] in OUTSIDE:
                out.append(f"adds a {p['audience']} page, “{p['title']}”")
        elif outside and _page_key(before) != _page_key(p):
            out.append(f"changes the {p['audience']} page “{p['title']}”")
        elif before.get("role") != p.get("role"):
            out.append(f"changes who opens “{p['title']}”: {before.get('role')} → {p.get('role')}")
    # Actions: who may run them, what a visitor submits, whom they email.
    ao, an = old.get("actions") or {}, new.get("actions") or {}
    for k in sorted(an):
        b, a = an[k], ao.get(k)
        if a is None:
            if b["role"] in OUTSIDE:
                out.append(f"adds “{b['label']}”, which {_who(b['role'])} can submit")
            continue
        if a["role"] != b["role"]:
            out.append(f"changes who can “{b['label']}”: {a['role']} → {b['role']}")
        elif b["role"] in OUTSIDE and sorted(a.get("input") or []) != sorted(b.get("input") or []):
            out.append(f"changes what {_who(b['role'])} submits with “{b['label']}”")
        if _recipients(a) != _recipients(b):
            out.append(f"changes who “{b['label']}” emails")
    # Private links.
    if (old.get("links") or {}) != (new.get("links") or {}):
        out.append("changes the private links the app sends (what their holders see or do)")
    # Retention.
    if (old.get("retention") or []) != (new.get("retention") or []):
        out.append("changes what candidate data is anonymized or deleted, and when")
    return out


def _readers(c: dict) -> str:
    also = c.get("read_also") or []
    return " + ".join([c.get("read") or "member", *also])


def _who(role: str) -> str:
    return "anyone on the public pages" if role == "public" else "a private link's holder"
