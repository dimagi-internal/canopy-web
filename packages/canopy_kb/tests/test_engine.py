"""The session does a model step's work (`canopy_kb.prep.engine`), and who did is not in the key."""
import json
import threading
import time
from pathlib import Path

import pytest
from canopy_kb.curate import Curation, CurationError, MemoryStore, Step
from canopy_kb.prep import engine, steps

CARD = {"summary": "A review of AMF.", "kind": "charity review", "orgs": ["Against Malaria Foundation"],
        "programs": ["insecticide-treated nets"], "countries": ["Malawi"], "period": "2019", "superseded": "",
        "answers": ["What does AMF cost per net?"]}


def _item(i="amf", text="Nets work."):
    return {"id": i, "title": "AMF review", "type": "page", "url": f"https://x/{i}", "date": "2025-09-01",
            "text": text, "sha256": __import__("hashlib").sha256(text.encode()).hexdigest()}


def test_the_model_is_metadata_and_what_it_made_before_is_still_found():
    """Jonathan, 2026-10-24: the model is just metadata. A card made when the model was in
    the key keeps its address among `keys`, so moving model (or engine) re-makes nothing."""
    step, it = steps.card_step("GiveWell"), _item()
    old = Step("card", "1", {**step.settings, "model": "gemini-2.5-flash"})  # the step as it was
    assert "model" not in step.settings
    assert steps.card_key(step, it, []) != old.key(**steps.card_inputs(it, []))
    assert steps.card_keys(step, it, [])[1] == old.key(**steps.card_inputs(it, []))
    other = Step(step.name, step.revision, step.settings, meta={"model": "another"})
    assert other.key(text="t") == step.key(text="t")
    a = steps.alias_step("GiveWell", "x -> y", "a, b")
    was = Step("aliases", "1", {**a.settings, "model": "gemini-2.5-pro"})
    assert a.keys(prompts=["p"])[1] == was.key(prompts=["p"])


def test_alias_groups_made_under_the_former_key_are_kept_not_asked_again():
    store = MemoryStore()
    step = steps.alias_step("GiveWell")
    cards = [{"orgs": ["AMF", "Against Malaria Foundation"], "programs": ["nets"]}]
    from canopy_kb.prep import aliases as _aliases
    p = _aliases.prompts(cards, "GiveWell")
    former = step.keys(prompts=[p[k][1] for k in sorted(p)])[1]
    groups = {"org": [{"canonical": "Against Malaria Foundation", "names": ["AMF", "Against Malaria Foundation"]}],
              "program": []}
    store.objects["results"][former] = __import__("canopy_kb.curate", fromlist=["encode"]).encode(groups)

    class Never:
        name = "never"

        def run(self, *a):
            raise AssertionError("asked again")

    with Curation(store) as cur:
        key, got = steps.aliases(cur, step, cards, engine=Never())
    assert got == groups and key in store.objects["results"]


def _answer(d, step, results, after=0.0):
    """A session, in another thread: waits for the tasks, then writes `results(tasks)`."""
    def go():
        p = d / f"{step}.tasks.json"
        while not p.exists():
            time.sleep(0.01)
        time.sleep(after)
        tasks = json.loads(p.read_text())
        (d / f"{step}.results.json.tmp").write_text(json.dumps(results(tasks)))
        (d / f"{step}.results.json.tmp").rename(d / f"{step}.results.json")
    t = threading.Thread(target=go)
    t.start()
    return t


def test_the_session_does_the_cards_and_each_is_stored_as_it_is_taken(tmp_path):
    store = MemoryStore()
    step = steps.card_step("GiveWell")
    todo = [(steps.card_key(step, _item(i), []), _item(i), []) for i in ("a", "b")]
    t = _answer(tmp_path, "card", lambda tasks: {x["id"]: CARD for x in tasks["tasks"]})
    with Curation(store) as cur:
        got = steps.cards(cur, step, todo, engine=engine.Session(tmp_path, timeout=10, poll=0.01))
    t.join()
    assert set(got) == {k for k, *_ in todo} and all(k in store.objects["results"] for k in got)
    assert json.loads((tmp_path / "card.finished.json").read_text()) == {"stored": 2, "left": 0}
    assert not (tmp_path / "card.tasks.json").exists()


def test_a_refused_card_is_asked_again_with_why_and_the_rest_are_kept(tmp_path):
    store = MemoryStore()
    step = steps.card_step("GiveWell")
    todo = [(steps.card_key(step, _item(i), []), _item(i), []) for i in ("a", "b")]
    ka, kb = todo[0][0], todo[1][0]
    first = _answer(tmp_path, "card", lambda tasks: {ka: CARD, kb: {"summary": ""}})
    seen = {}

    def second():
        first.join()
        p = tmp_path / "card.tasks.json"
        while not p.exists() or json.loads(p.read_text())["count"] != 1:
            time.sleep(0.01)
        seen.update(json.loads(p.read_text()))
        (tmp_path / "card.results.json").write_text(json.dumps({kb: CARD}))
    t = threading.Thread(target=second)
    t.start()
    with Curation(store) as cur:
        got = steps.cards(cur, step, todo, engine=engine.Session(tmp_path, timeout=10, poll=0.01))
    t.join()
    assert set(got) == {ka, kb}
    assert [x["id"] for x in seen["tasks"]] == [kb] and "summary" in seen["errors"][kb]


def test_the_session_can_give_up_and_what_is_left_gets_no_result(tmp_path):
    store = MemoryStore()
    step = steps.card_step("GiveWell")
    todo = [(steps.card_key(step, _item("a"), []), _item("a"), [])]
    (tmp_path / "card.done").write_text("")  # stale from a run before: cleared at the start

    def give_up():
        while not (tmp_path / "card.tasks.json").exists():
            time.sleep(0.01)
        (tmp_path / "card.done").write_text("")
    t = threading.Thread(target=give_up)
    t.start()
    with Curation(store) as cur:
        got = steps.cards(cur, step, todo, engine=engine.Session(tmp_path, timeout=10, poll=0.01))
    t.join()
    assert got == {} and json.loads((tmp_path / "card.finished.json").read_text()) == {"stored": 0, "left": 1}


def test_aliases_with_no_answer_stop_the_refresh_and_say_so(tmp_path):
    with Curation(MemoryStore()) as cur, pytest.raises(CurationError, match="no groups for"):
        steps.aliases(cur, steps.alias_step("GiveWell"), [{"orgs": ["AMF"], "programs": []}],
                      engine=engine.Session(tmp_path, timeout=0.05, poll=0.01))


def test_split_and_merge_fan_a_batch_out_and_back(tmp_path):
    tasks = [{"id": f"k{n}", "prompt": "p"} for n in range(5)]
    (tmp_path / "card.tasks.json").write_text(json.dumps({"step": "card", "instructions": "i", "count": 5,
                                                          "tasks": tasks, "errors": {"k1": "no summary"}}))
    parts = engine.split(tmp_path, "card", size=2)
    assert [json.loads(p.read_text())["count"] for p in parts] == [2, 2, 1]
    assert json.loads(parts[0].read_text())["errors"] == {"k1": "no summary"}
    for p in parts:
        part = json.loads(p.read_text())
        (tmp_path / Path(part["results"]).name).write_text(json.dumps({t["id"]: CARD for t in part["tasks"]}))
    assert engine.merge(tmp_path, "card") == {"results": 5, "unreadable_parts": []}
    assert len(json.loads((tmp_path / "card.results.json").read_text())) == 5
    assert not list(tmp_path.glob("card.part-*"))
    assert engine.status(tmp_path)["waiting"] == {"card": 5}


def _reader(kind, body, page_selector=None, title_suffix=""):
    """A reader that dies (as an out-of-memory kill does) on the file that says so."""
    import os
    if body == b"huge":
        os._exit(9)
    return {"text": body.decode()}


class _Fetch:
    def __init__(self, bodies):
        self.bodies = bodies

    def prefetch(self, rows):
        pass

    def body(self, r):
        return self.bodies[r["url"]]

    def forget_bodies(self):
        pass


def test_a_reader_that_dies_loses_only_its_own_file_and_the_run_goes_on(monkeypatch):
    import multiprocessing
    if multiprocessing.get_start_method() != "fork":
        pytest.skip("the stub reader reaches the pool's processes by fork")
    from canopy_kb.prep import extract as _extract
    monkeypatch.setattr(_extract, "extract_bytes", _reader)
    bodies = {f"https://x/{n}": f"text {n}".encode() for n in range(6)}
    bodies["https://x/huge"] = b"huge"
    rows = [{"url": u, "sha256": u} for u in bodies]
    step = steps.reader("pdf")
    with Curation(MemoryStore()) as cur:
        got = steps.extract(cur, _Fetch(bodies), [(r, step) for r in rows], workers=3)
    assert sorted(v["text"] for v in got.values()) == [f"text {n}" for n in range(6)]


def test_the_reader_pool_fits_the_machine():
    assert 1 <= steps.reader_workers() <= 8
