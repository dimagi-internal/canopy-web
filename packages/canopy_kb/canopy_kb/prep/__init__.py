"""The curator's toolkit: from a publisher's raw files to the bundle `build` reads.

The engine (`canopy_kb`) is stdlib-only; this subpackage needs the readers
(`pip install 'canopy-kb[prep]'`). A curator's adapters keep only what is particular to
one publisher (where it puts things, how its items are typed, its tables and guide) and
call these for the rest:

| module | what |
|---|---|
| `raw.py` | the raw HTTP cache every stage reads (`Raw`), and file helpers |
| `extract.py` | each format to markdown (`html`, `pdf`, `xlsx` readers), cached by sha256 |
| `dedupe.py` | identical documents once; shared spreadsheet tabs once |
| `cards.py` | a catalog card per item: its prompt and the shape a card is held to |
| `aliases.py` | entity names that are one thing: their prompt and the groups kept |
| `engine.py` | the AI session running the refresh reads those prompts: the task hand-over and its helpers |
| `originals.py` | each item's original file, for `canopy kb push --originals` |
"""
