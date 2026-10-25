"""A refresh with no local state (`canopy_kb.curate`), against a host in memory."""
import json

import pytest
from canopy_kb import curate, records
from canopy_kb.curate import Curation, MemoryStore, StaleStep, State, Step


def _doc(i, text, **kw):
    item = {"id": i, "type": "page", "title": i, "url": f"https://x/{i}", "text": text,
            "sha256": __import__("hashlib").sha256(text.encode()).hexdigest(), **kw}
    return records.record(item), text


def _publish(cur, docs, parts=None, **kw):
    recs = {i: r for i, (r, _) in docs.items()}
    return cur.publish(recs, lambda ids: {i: docs[i][1] for i in ids}, parts or {"manifest": {"name": "t"}}, **kw)


def test_a_step_key_names_its_code_and_its_inputs_not_the_machine():
    a = Step("card", "1", {"prompt": "Describe {x}"})
    assert a.key(text="t1") == Step("card", "1", {"prompt": "Describe {x}"}).key(text="t1")
    assert a.key(text="t1") != a.key(text="t2")
    assert a.key(text="t1") != Step("card", "2", {"prompt": "Describe {x}"}).key(text="t1")  # a revision
    assert a.key(text="t1") != Step("card", "1", {"prompt": "Summarise {x}"}).key(text="t1")  # a new prompt


def test_the_lock_refuses_changed_code_until_its_author_decides(tmp_path):
    src = tmp_path / "reader.py"
    src.write_text("def read(): return 1\n")
    lock = tmp_path / "steps.lock"
    step = Step("extract:x", "1", sources=(str(src),), libraries=("pytest",))
    with pytest.raises(StaleStep, match="not in"):
        curate.check_lock([step], lock)
    curate.write_lock([step], lock)
    curate.check_lock([step], lock)
    src.write_text("def read(): return 2\n")
    with pytest.raises(StaleStep, match="its code changed"):
        curate.check_lock([step], lock)
    bumped = Step("extract:x", "2", sources=(str(src),), libraries=("pytest",))
    with pytest.raises(StaleStep, match="REVISION is 2"):
        curate.check_lock([bumped], lock)
    curate.write_lock([bumped], lock)  # the author's decision, recorded
    curate.check_lock([bumped], lock)
    entry = json.loads(lock.read_text())["extract:x"]
    entry["libraries"]["pytest"] = "0.0"
    lock.write_text(json.dumps({"extract:x": entry}))
    with pytest.raises(StaleStep, match="reader libraries moved"):
        curate.check_lock([bumped], lock)


def test_the_state_is_saved_in_chunks_and_an_unchanged_chunk_is_not_sent_again():
    store = MemoryStore()
    st = State({"fetch": {f"https://x/{n}": {"status": 200, "sha256": str(n)} for n in range(500)}})
    root = st.upload(store)
    first = len(store.objects["results"])
    store.versions.append({"number": 1, "file": b"", "curation": root})  # the version it was built for
    again = State.load(store)
    assert again.sections == st.sections and again.root == root and again.version == 1
    again.section("fetch")["https://x/7"]["sha256"] = "changed"
    again.upload(store)
    assert len(store.objects["results"]) == first + 2  # one chunk and the root


def test_a_result_is_computed_once_on_any_machine():
    store = MemoryStore()
    calls = []
    with Curation(store) as cur:
        assert cur.memo("k" * 64, lambda: calls.append(1) or {"text": "read"}) == {"text": "read"}
    with Curation(store) as cur:  # another machine, empty disk
        assert cur.memo("k" * 64, lambda: calls.append(1) or {"text": "again"}) == {"text": "read"}
    assert calls == [1]


def test_a_model_result_is_stored_the_moment_it_is_made():
    store = MemoryStore()
    cur = Curation(store).__enter__()
    cur.put("c" * 64, {"card": 1}, now=True)
    assert "c" * 64 in store.objects["results"]  # a run that dies now has not lost it


def test_a_version_carries_its_state_and_an_unchanged_refresh_sends_and_saves_nothing():
    store = MemoryStore()
    docs = {"a": _doc("a", "Alpha " * 500), "b": _doc("b", "Beta " * 500)}
    with Curation(store) as cur:
        cur.state.section("fetch")["https://x/a"] = {"status": 200}
        out = _publish(cur, docs)
    assert out["published"] and out["version"] == 1 and store.versions[0]["curation"]
    assert store.item("a")["text"] == "Alpha " * 500

    with Curation(store) as cur:  # another machine: it starts from version 1's state
        assert cur.state.version == 1 and cur.state.section("fetch") == {"https://x/a": {"status": 200}}
        cur.state.section("fetch")["https://x/a"]["etag"] = "e2"  # a refetch that found the same content
        out = _publish(cur, docs)
    assert out["published"] is False and out["unchanged"] and len(store.versions) == 1
    assert not any(c.startswith(("publish", "put results")) and not c.endswith(" 0") for c in store.calls[-2:])

    with Curation(store) as cur:  # a new card on "a": a version, but no news for readers
        rec, text = _doc("a", "Alpha " * 500, card={"summary": "new"})
        out = _publish(cur, {**docs, "a": (rec, text)})
    assert out["published"] and out["version"] == 2 and store.item("a")["card"]["summary"] == "new"
    assert store.item("b")["text"] == "Beta " * 500 and store.versions[-1]["changes"]["changed"] == 0

    with Curation(store) as cur:  # a new text is news
        _publish(cur, {**docs, "a": _doc("a", "Alpha, revised " * 500)})
    assert store.versions[-1]["changes"]["changed"] == 1


def test_a_refresh_that_would_remove_much_stops():
    store = MemoryStore()
    docs = {str(n): _doc(str(n), f"text {n}") for n in range(100)}
    with Curation(store) as cur:
        _publish(cur, docs)
    with Curation(store) as cur, pytest.raises(curate.CurationError, match="remove 10 of 100"):
        _publish(cur, {k: v for k, v in docs.items() if int(k) >= 10})
    assert len(store.versions) == 1
    with Curation(store) as cur:
        assert _publish(cur, {k: v for k, v in docs.items() if int(k) >= 10}, allow_removals=True)["removed"] == 10


def test_two_refreshes_from_one_version_the_second_to_publish_loses_and_keeps_what_it_paid_for():
    store = MemoryStore()
    with Curation(store) as cur:
        _publish(cur, {"a": _doc("a", "one")})
    one, two = Curation(store).__enter__(), Curation(store).__enter__()
    two.put("c" * 64, {"card": "paid"}, now=True)
    _publish(two, {"a": _doc("a", "two")})
    one.put("d" * 64, {"card": "paid too"}, now=True)
    with pytest.raises(curate.CurationError, match="at version 2, not 1"):
        _publish(one, {"a": _doc("a", "three")})
    assert len(store.versions) == 2 and store.item("a")["text"] == "two"
    assert {"c" * 64, "d" * 64} <= set(store.objects["results"])  # neither result is lost


def test_a_rollback_rolls_the_state_back_too():
    store = MemoryStore()
    with Curation(store) as cur:
        cur.state.section("x")["k"] = 1
        _publish(cur, {"a": _doc("a", "one")})
    with Curation(store) as cur:
        cur.state.section("x")["k"] = 2
        _publish(cur, {"a": _doc("a", "two")})
    store.rollback(1)
    with Curation(store) as cur:
        assert cur.state.version == 3 and cur.state.section("x") == {"k": 1}
        assert _publish(cur, {"a": _doc("a", "one")})["unchanged"]  # version 1's content, as served


# ---- fetching from the curation state (canopy_kb.prep.fetch) ----------------------------

class _Resp:
    def __init__(self, status, body=b"", headers=None, url="https://x/a"):
        self.status_code, self.content, self.headers, self.url = status, body, headers or {}, url


class _Client:
    def __init__(self, answers):
        self.answers, self.sent = answers, []

    def get(self, url, headers=None):
        self.sent.append((url, dict(headers or {})))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def _fetcher(cur, answers, monkeypatch):
    pytest.importorskip("httpx")
    from canopy_kb.prep import fetch
    f = fetch.Fetch(cur)
    client = _Client(answers)
    monkeypatch.setattr(f, "_client", lambda: client)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    return f, client


def test_a_fresh_machine_asks_the_publisher_only_what_the_last_one_would_have(monkeypatch):
    store = MemoryStore()
    with Curation(store) as cur:
        f, client = _fetcher(cur, [_Resp(200, b"<html>v1</html>", {"etag": "e1", "content-type": "text/html"})],
                             monkeypatch)
        row = f.get("https://x/a")
        assert row["sha256"] and row["etag"] == "e1"
        f.flush()
        _publish(cur, {"a": _doc("a", "v1")})
    with Curation(store) as cur:  # empty disk: the row is in the state, the bytes in the store
        f, client = _fetcher(cur, [_Resp(304)], monkeypatch)
        assert f.get("https://x/a")["sha256"] == row["sha256"] and client.sent == []  # not asked: no refetch
        again = f.get("https://x/a", refetch=True)
        assert client.sent[0][1]["If-None-Match"] == "e1" and again["sha256"] == row["sha256"]
        assert f.body(again) == b"<html>v1</html>" and f.new_originals() == {}  # from the originals store
        assert store.calls[-1].startswith("get originals")


def test_an_outage_keeps_the_last_good_copy_and_a_404_is_a_removal(monkeypatch):
    store = MemoryStore()
    with Curation(store) as cur:
        f, _ = _fetcher(cur, [_Resp(200, b"x", {"content-type": "application/pdf"}), _Resp(503), _Resp(503), _Resp(503),
                              _Resp(503), _Resp(503), _Resp(404)], monkeypatch)
        good = f.get("https://x/a")
        kept = f.get("https://x/a", refetch=True)
        assert kept["sha256"] == good["sha256"] and kept["status"] == 200 and kept["last_error"] == 503
        assert f.get("https://x/a", refetch=True)["status"] == 404


def test_a_google_export_that_says_the_same_keeps_its_row_and_original(monkeypatch):
    import io
    import zipfile

    def xlsx(comment_id: str) -> bytes:
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("xl/worksheets/sheet1.xml", "<sheet>same cells</sheet>")
            z.writestr("xl/drawings/vmlDrawing1.vml", f"<v id='{comment_id}'/>")
        return b.getvalue()

    ct = {"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    store = MemoryStore()
    with Curation(store) as cur:
        f, _ = _fetcher(cur, [_Resp(200, xlsx("1"), ct), _Resp(200, xlsx("2"), ct)], monkeypatch)
        first = f.get("https://docs.google.com/spreadsheets/d/S/export?format=xlsx")
        again = f.get("https://docs.google.com/spreadsheets/d/S/export?format=xlsx", refetch=True)
        assert again["sha256"] == first["sha256"] and first["stable"] and f.counts["same_content"] == 1
        assert len(f.new_originals()) == 1 and f.flush() == 1 and f.new_originals() == {}


def test_the_toolkits_lock_describes_its_code():
    """A reader, card or alias change lands with its REVISION decision (see
    canopy_kb/prep/steps.py): bump it if the output can change, then relock."""
    pytest.importorskip("pymupdf")
    from canopy_kb.prep import steps
    steps.check()


def test_urls_keep_the_order_they_were_first_fetched_in_across_machines(monkeypatch):
    store = MemoryStore()
    with Curation(store) as cur:
        f, _ = _fetcher(cur, [_Resp(200, f"<p>{n}</p>".encode(), {"content-type": "text/html"}) for n in range(40)],
                        monkeypatch)
        urls = [f"https://x/{n}" for n in reversed(range(40))]
        for u in urls:
            f.get(u)
        f.flush()
        _publish(cur, {"a": _doc("a", "one")})
    with Curation(store) as cur:  # the state comes back in chunk order; the fetcher restores fetch order
        from canopy_kb.prep import fetch
        assert list(cur.state.section("fetch")) != urls
        assert [u for u, _ in fetch.Fetch(cur).in_order()] == urls


def test_the_refresh_builds_the_index_and_it_is_the_full_build_of_the_same_content(tmp_path):
    """The host never builds: the refresh applies what changed to the file the host serves
    (`canopy_kb.apply`), which is the full build of the same content."""
    import json as _json

    from canopy_kb import KB, build
    store = MemoryStore()
    docs = {str(n): _doc(str(n), f"Report {n} on nets and water. " * 20) for n in range(300)}
    with Curation(store) as cur:
        assert _publish(cur, docs)["version"] == 1
    later = {**docs, "7": _doc("7", "A new text about chlorination. " * 20)}
    later.pop("8")
    with Curation(store) as cur:
        out = _publish(cur, later, allow_removals=True)
    assert out["version"] == 2 and (out["changed"], out["removed"]) == (1, 1)
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(_json.dumps({"name": "t"}))
    (bundle / "items.jsonl").write_text("".join(_json.dumps(records.join(r, t)) + "\n" for r, t in later.values()))
    build(bundle, tmp_path / "full.sqlite")
    served = tmp_path / "served.sqlite"
    store.served(served)
    a, f = KB(served), KB(tmp_path / "full.sqlite")
    same = lambda s: {k: v for k, v in s.items() if k not in ("built_at", "new", "changed", "removed", "baseline")}  # noqa: E731
    assert a.summary["items"] == 299 and same(a.summary) == same(f.summary)
    for iid in ("7", "42", "299"):
        assert a.text(iid) == f.text(iid) and dict(a.item_row(iid))["sha256"] == dict(f.item_row(iid))["sha256"]
    assert a.item_row("8") is None and "chlorination" in a.find("chlorination")
    assert store.item("7")["text"].startswith("A new text")


def _xlsx(strings, styles, value="0.25", fmt="0%", note="GiveWell estimate"):
    """A workbook written as Google writes one: cells pointing into a shared-string table
    and a style table, each in the order given."""
    import io
    import zipfile
    m = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pr = "http://schemas.openxmlformats.org/package/2006/relationships"
    sst = "".join(f"<si><t>{t}</t></si>" for t in strings)
    xfs = "".join(f'<xf numFmtId="{164 if f == fmt else 0}"/>' for f in styles)
    pct, txt = styles.index(fmt), styles.index("General")
    cells = (f'<row r="1"><c r="A1" t="s" s="{txt}"><v>{strings.index("Coverage")}</v></c>'
             f'<c r="B1" s="{pct}"><v>{value}</v></c></row>'
             f'<row r="2"><c r="A2" t="s" s="{txt}"><v>{strings.index("Cost per child")}</v></c>'
             f'<c r="B2"><f>B1*3</f><v>0.75</v></c></row>')
    files = {
        "xl/workbook.xml": f'<workbook xmlns="{m}" xmlns:r="{r}"><sheets><sheet name="Inputs" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{pr}"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{m}"><sheetData>{cells}</sheetData></worksheet>',
        "xl/sharedStrings.xml": f'<sst xmlns="{m}">{sst}</sst>',
        "xl/styles.xml": f'<styleSheet xmlns="{m}"><numFmts><numFmt numFmtId="164" formatCode="{fmt}"/></numFmts><cellXfs>{xfs}</cellXfs></styleSheet>',
        "xl/comments1.xml": f'<comments xmlns="{m}"><commentList><comment ref="B1"><text><t>{note}</t></text></comment></commentList></comments>',
    }
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return b.getvalue()


def test_a_workbook_is_the_same_when_its_cells_are_whatever_its_tables_order():
    """Google re-exports an unchanged Sheet with its string and style tables renumbered;
    a value, number format or note that changes is a change."""
    from canopy_kb.prep.fetch import cells_hash
    a = _xlsx(["Coverage", "Cost per child"], ["General", "0%"])
    b = _xlsx(["Cost per child", "Coverage"], ["0%", "General"])  # the same cells, tables reordered
    assert a != b and cells_hash(a) == cells_hash(b) != ""
    assert cells_hash(_xlsx(["Coverage", "Cost per child"], ["General", "0%"], value="0.3")) != cells_hash(a)
    assert cells_hash(_xlsx(["Coverage", "Cost per child"], ["General", "0.0%"], fmt="0.0%")) != cells_hash(a)
    assert cells_hash(_xlsx(["Coverage", "Cost per child"], ["General", "0%"], note="revised")) != cells_hash(a)
    assert cells_hash(b"PK not a workbook") == ""
