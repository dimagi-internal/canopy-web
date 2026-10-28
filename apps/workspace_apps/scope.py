"""Record-level access: which records of a SCOPED collection a member may see.

A collection's `read` role says who may read the collection at all. A `scope`
narrows that to records: Hiring's jobs are seen by everyone, but a confidential
job only by its hiring team, and a job's applications only by the people
working on that job. The manifest declares it; nothing here names a collection.

    "scope": {
      "bypass": ["recruiter"],           # these roles see every record (root only; children inherit)
      "team": ["hiring_manager", ...],   # email/text fields naming who works on THIS record
      "private_field": "confidential",   # root only: a true value hides it from everyone off its team
      "via": "job",                      # child only: a ref to the scoped parent
      "team_only_setting": "team_only_access",  # child: when on, the parent's TEAM, not its viewers
      "shared_by": [{"collection": "interview", "ref": "application", "team": ["interviewers"]}]
    }

A record is visible to a member who is on its team, or to whom another record
shares it (an interviewer sees the application their interview names), or —
for a root — when it is not private, or — for a child — when its parent is
(the parent's team, when `team_only_setting` is on). `shared_by` lets a person
SEE a record; it never makes them its team, so an interviewer on one interview
does not inherit the rest of the job.

Who is checked: a member's session. The app itself (then-steps, scenarios,
retention), a public visitor and a private-link holder are not scoped here —
their doors carry their own rules.
"""
from __future__ import annotations

import re

from .models import AppRecord

MAX_DEPTH = 4


def _people(data: dict, fields: list[str]) -> set[str]:
    out = set()
    for f in fields:
        out |= {p.strip().lower() for p in re.split(r"[,;\s]+", str(data.get(f) or "")) if "@" in p}
    return out


def validate(collections: dict, raw_collections: dict, roles: set[str], settings: dict, fail) -> None:
    """Check every `scope` and store its normalised form on the collection."""
    for name, raw in raw_collections.items():
        spec = (raw or {}).get("scope") if isinstance(raw, dict) else None
        if spec is None:
            continue
        where = f"collections.{name}.scope"
        if not isinstance(spec, dict):
            fail(where, "must be an object")
        fields = collections[name]["fields"]
        out: dict = {}
        team = spec.get("team") or []
        if not isinstance(team, list) or any(f not in fields or fields[f]["type"] not in ("email", "text")
                                             for f in team):
            fail(f"{where}.team", "must list email or text fields of the collection")
        out["team"] = list(team)
        via = spec.get("via")
        if via is not None:
            if via not in fields or fields[via]["type"] != "ref":
                fail(f"{where}.via", "must be a ref field")
            if spec.get("private_field") or spec.get("bypass"):
                fail(where, "a child scope takes its bypass from its root and has no private_field")
            out["via"] = via
            tos = spec.get("team_only_setting")
            if tos is not None:
                if tos not in settings or settings[tos]["type"] != "bool":
                    fail(f"{where}.team_only_setting", "must name a bool setting")
                out["team_only_setting"] = tos
        else:
            bypass = spec.get("bypass")
            if not isinstance(bypass, list) or not bypass or any(r not in roles or r in ("public", "member")
                                                                 for r in bypass):
                fail(f"{where}.bypass", "must list roles of this app (not public or member)")
            out["bypass"] = list(bypass)
            pf = spec.get("private_field")
            if pf is not None:
                if pf not in fields or fields[pf]["type"] != "bool":
                    fail(f"{where}.private_field", "must be a bool field")
                out["private_field"] = pf
        shared = []
        for i, sb in enumerate(spec.get("shared_by") or []):
            w = f"{where}.shared_by[{i}]"
            if not isinstance(sb, dict) or sb.get("collection") not in collections:
                fail(f"{w}.collection", "must name a collection")
            sf = collections[sb["collection"]]["fields"]
            if sb.get("ref") not in sf or sf[sb["ref"]]["type"] != "ref" or sf[sb["ref"]]["collection"] != name:
                fail(f"{w}.ref", f"must be a ref field of '{sb['collection']}' pointing at '{name}'")
            steam = sb.get("team")
            if not isinstance(steam, list) or not steam or any(f not in sf or sf[f]["type"] not in ("email", "text")
                                                               for f in steam):
                fail(f"{w}.team", "must list email or text fields")
            shared.append({"collection": sb["collection"], "ref": sb["ref"], "team": list(steam)})
        out["shared_by"] = shared
        collections[name]["scope"] = out
    for name, c in collections.items():
        seen, cur = {name}, c
        while cur.get("scope", {}).get("via"):
            parent = cur["fields"][cur["scope"]["via"]]["collection"]
            if parent in seen or "scope" not in collections[parent] or len(seen) > MAX_DEPTH:
                fail(f"collections.{name}.scope.via", "must lead, within four steps and without a loop, "
                                                      "to a scoped root")
            seen.add(parent)
            cur = collections[parent]


class Scope:
    """One caller's view of one installation's space. Answers are cached, so a
    request that checks many records reads each collection once."""

    def __init__(self, inst, m: dict, space: str, user, roles: set[str]):
        self.inst, self.m, self.space, self.roles = inst, m, space, roles
        self.email = (getattr(user, "email", "") or "").lower()
        self._data: dict[str, list[tuple[int, dict]]] = {}
        self._allowed: dict[tuple[str, str], set[int] | None] = {}

    def _root(self, coll: str) -> dict:
        c = self.m["collections"][coll]
        while c["scope"].get("via"):
            c = self.m["collections"][c["fields"][c["scope"]["via"]]["collection"]]
        return c["scope"]

    def _records(self, coll: str) -> list[tuple[int, dict]]:
        if coll not in self._data:
            self._data[coll] = list(AppRecord.objects.filter(installation=self.inst, space=self.space,
                                                             collection=coll).values_list("pk", "data"))
        return self._data[coll]

    def _shared(self, coll: str, spec: dict) -> set[int]:
        out = set()
        for sb in spec["shared_by"]:
            for _pk, d in self._records(sb["collection"]):
                if self.email in _people(d, sb["team"]) and isinstance(d.get(sb["ref"]), int):
                    out.add(d[sb["ref"]])
        return out

    def allowed(self, coll: str, level: str = "visible") -> set[int] | None:
        """The ids of `coll` this caller may see (`level="team"`: whose team they
        are on), or None when nothing narrows it."""
        c = self.m["collections"].get(coll) or {}
        spec = c.get("scope")
        if not spec or self.roles & set(self._root(coll)["bypass"]):
            return None
        key = (coll, level)
        if key in self._allowed:
            return self._allowed[key]
        recs = self._records(coll)
        own = {pk for pk, d in recs if self.email and self.email in _people(d, spec["team"])}
        if spec.get("via"):
            tos = spec.get("team_only_setting")
            s = (self.m.get("settings") or {}).get(tos or "") or {}
            on = bool(s.get("value", s.get("default")))
            parent = self.allowed(c["fields"][spec["via"]]["collection"], "team" if on else "visible")
            if parent is None:
                result = None
            else:
                result = own | self._shared(coll, spec) | {pk for pk, d in recs if d.get(spec["via"]) in parent}
        elif level == "team":
            result = own
        elif not spec.get("private_field"):
            result = None
        else:
            pf = spec["private_field"]
            result = own | self._shared(coll, spec) | {pk for pk, d in recs if not d.get(pf)}
        self._allowed[key] = result
        return result

    def sees(self, coll: str, record_id: int) -> bool:
        ids = self.allowed(coll)
        return ids is None or record_id in ids
