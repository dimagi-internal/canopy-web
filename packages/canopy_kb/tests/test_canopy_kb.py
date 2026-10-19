"""The engine and its MCP tool layer, with plain pytest (no Django)."""
import json

import pytest

from canopy_kb import KB, KBError, build
from canopy_kb.tools import run, tool_specs


def _bundle(tmp_path):
    b = tmp_path / "bundle"
    (b / "tables").mkdir(parents=True)
    (b / "manifest.json").write_text(json.dumps({
        "name": "acme", "publisher": "Acme Foundation", "license": "CC BY 4.0",
        "tables": {"grants": {"description": "Every grant.", "date_column": "Date",
                              "amount_column": "Amount", "filter_columns": ["Recipient"]}}}))
    (b / "items.jsonl").write_text("\n".join(json.dumps(i) for i in [
        {"id": "p:1", "type": "podcast_episode", "title": "Diarrheal disease", "url": "https://x/1",
         "date": "2026-07-23", "text": "Cholera spreads through contaminated water.", "sha256": "a"},
        {"id": "b:1", "type": "blog_post", "title": "Safe water", "url": "https://x/2",
         "date": "2025-10-29", "text": "Chlorination pilots in six countries.", "sha256": "b"},
    ]))
    (b / "tables" / "grants.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"Grant": "Water pilot", "Recipient": "Dimagi Inc", "Amount": 1069324, "Date": "2025-12-03"},
        {"Grant": "Cholera RCT", "Recipient": "Johns Hopkins", "Amount": 740851.8, "Date": "2025-03-14"},
    ]))
    return b


@pytest.fixture()
def kb(tmp_path):
    db = tmp_path / "kb.sqlite"
    build(_bundle(tmp_path), db)
    return KB(db)


def test_every_table_becomes_a_named_tool_beside_the_base_tools(kb):
    names = [t["name"] for t in tool_specs(kb)]
    assert names[:6] == ["kb_info", "search", "get", "list_items", "whats_new", "table"]
    assert "grants" in names
    grants = next(t for t in tool_specs(kb) if t["name"] == "grants")
    assert {"Recipient", "since", "min_amount"} <= set(grants["inputSchema"]["properties"])


def test_tools_answer_with_the_source_url(kb):
    hit = run(kb, "search", {"query": "cholera"})[0]
    assert hit["url"] == "https://x/1"
    rows = run(kb, "grants", {"Recipient": "Dimagi"})
    assert rows["matched"] == 1 and rows["rows"][0]["Grant"] == "Water pilot"
    assert run(kb, "kb_info", {})["manifest"]["publisher"] == "Acme Foundation"


def test_an_unknown_tool_is_refused(kb):
    with pytest.raises(KBError):
        run(kb, "drop_tables", {})
