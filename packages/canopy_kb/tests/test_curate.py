"""A refresh with no local state (`canopy_kb.curate`), against a host in memory."""
import json

import pytest
from canopy_kb import curate, records
from canopy_kb.curate import Budget, Curation, MemoryStore, OverBudget, StaleStep, State, Step


def _doc(i, text, **kw):
    item = {"id": i, "type": "page", "title": i, "url": f"https://x/{i}", "text": text, "sha256": i + text[:3], **kw}
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
    store.kb_curation["sha"] = root
    again = State.load(store)
    assert again.sections == st.sections and again.root == root
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


def test_the_budget_stops_a_run_before_its_first_model_call():
    b = Budget(2)
    b.plan(Step("card", "1", cost=0.001), 10904)
    b.plan(Step("ocr", "1", cost=0.03), 10)
    with pytest.raises(OverBudget, match=r"card: 10904 calls .*--budget 12"):
        b.check()
    Budget(12).check()


def test_publishing_sends_what_changed_a_card_change_sends_no_text_and_the_state_goes_with_it():
    store = MemoryStore()
    docs = {"a": _doc("a", "Alpha " * 500), "b": _doc("b", "Beta " * 500)}
    with Curation(store) as cur:
        cur.state.section("fetch")["https://x/a"] = {"status": 200}
        out = _publish(cur, docs)
    assert out["published"] and out["version"] == 1 and out["state_saved"]
    assert store.kb_curation["sha"] and store.kb_curation["version"] == 1
    assert store.item("a")["text"] == "Alpha " * 500
    sent = len(store.objects["records"])

    with Curation(store) as cur:  # nothing new: nothing sent, no version, state unchanged
        out = _publish(cur, docs)
    assert out == {**out, "published": False, "unchanged": True, "objects_sent": 0, "state_saved": False}
    assert len(store.versions) == 1

    with Curation(store) as cur:  # a new card on "a": its small record only
        rec, text = _doc("a", "Alpha " * 500, card={"summary": "new"})
        out = _publish(cur, {**docs, "a": (rec, text)})
    assert out["published"] and out["changed"] == 1 and out["objects_sent"] == 1
    assert len(store.objects["records"]) == sent + 1 and store.item("a")["card"] == {"summary": "new"}


def test_a_refresh_that_would_remove_much_stops_and_a_second_refresh_waits():
    store = MemoryStore()
    docs = {str(n): _doc(str(n), f"text {n}") for n in range(100)}
    with Curation(store) as cur:
        _publish(cur, docs)
    with Curation(store) as cur, pytest.raises(curate.CurationError, match="remove 10 of 100"):
        _publish(cur, {k: v for k, v in docs.items() if int(k) >= 10})
    assert len(store.versions) == 1
    with Curation(store) as cur:
        assert _publish(cur, {k: v for k, v in docs.items() if int(k) >= 10}, allow_removals=True)["removed"] == 10
    with Curation(store, holder="a"), pytest.raises(curate.CurationError, match="holds the lease"):
        Curation(store, holder="b").__enter__()


def test_a_stale_state_cannot_be_committed():
    store = MemoryStore()
    with Curation(store) as cur:
        cur.state.section("x")["k"] = 1
        _publish(cur, {"a": _doc("a", "one")})
    one, two = Curation(store, holder="1").__enter__(), None
    store.drop_lease("1")
    two = Curation(store, holder="2").__enter__()
    two.state.section("x")["k"] = 2
    _publish(two, {"a": _doc("a", "two")})
    store.drop_lease("2")
    store.take_lease("1", 60)
    one.state.section("x")["k"] = 3
    with pytest.raises(curate.CurationError, match="saved since"):
        _publish(one, {"a": _doc("a", "three")})
    assert len(store.versions) == 2 and store.item("a")["text"] == "two"


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
    """A reader, OCR, card or alias change lands with its REVISION decision (see
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


def test_a_large_publish_asks_the_host_a_batch_at_a_time():
    """A first publish in the new record format sends every text: the host is asked which
    it holds once per thousand, not once per text (10,868 texts made it a 502)."""
    store = MemoryStore()
    docs = {str(n): _doc(str(n), f"text {n}") for n in range(2500)}
    with Curation(store) as cur:
        _publish(cur, docs)
    asks = [c for c in store.calls if c.startswith("have records")]
    assert len(asks) <= 6, len(asks)  # records once, texts once, a batch of 1000 each

