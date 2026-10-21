# canopy-kb

A knowledge base is everything one publisher has made public, kept as dated, cited
copies with the tables you compute with alongside. A curator agent writes a bundle
(`manifest.json`, `items.jsonl` with a catalog `card` per item, `tables/*.jsonl`,
optional `aliases.json`); `build` turns it into `kb.sqlite`; `KB` queries it; `tools`
describes it as MCP tools. canopy-web serves those tools per workspace project at
`/api/mcp/projects/<workspace>/<handle>/`. Contract: canopy `docs/knowledge-bases.md`.

The tools are for an agent doing research: `kb_info` (what the library holds), `search`
(documents with their cards and best passages), `entity` (everything about an
organisation or program), `doc` (outline, links, versions), `read` (a section),
`grep` (a figure or quote in a long document), `sql` (the declared tables) and
`original` (the file a document was read from).

| module | what |
|---|---|
| `chunking.py` | passages (3,200-char spans with a heading path, 400 overlap) and outlines |
| `entities.py` | the entity index from catalog cards, merged by key, acronym and alias |
| `kb.py` | `build` (schema 3) and `KB`: the reader the research tools run on |
| `research.py` | the research tools, as text |
| `vectors.py` | dormant: what a passage's vector would be made of, for a host that turns vectors on |
| `tools.py` | MCP tool specs and dispatch |
| `prep/` | the curator's toolkit: raw cache, a reader per format, OCR, dedupe, cards, aliases, originals (extra `[prep]`) |

**Originals.** An item's text is a reading copy of a file (a PDF, a spreadsheet, a page):
it is what search, `read` and `grep` work on. The file stays the record. A curator puts
it on the item as `original` (`prep/originals.py`), `canopy kb push --originals` uploads
each one to the host's store by sha256, and the `original` tool hands a reader a download
link: the workbook to recompute, the PDF's charts, or a version the publisher has since
changed or removed (a build carries superseded originals forward; the store never
deletes one).

**Curators.** A curator's own code keeps only what is particular to its publisher (where
it puts things, how items are typed, its tables and guide) and calls `canopy_kb.prep`
for the rest: `pip install 'canopy-kb[prep] @ git+ssh://git@github.com/dimagi-internal/canopy-web.git#subdirectory=packages/canopy_kb'`.
Eva's GiveWell adapters (`lib/kb_givewell`) are the worked example.

No embeddings are involved. `search` is words over passages and catalog cards, ordered
by the host's reranker; the agent then opens and greps documents. On 70 GiveWell
questions that beat one-shot embedding search (35.5/40 fact questions against 28.5).
`vectors.py`, `KB.passages()` and `search`'s `semantic` hook stay so a host can turn
vectors back on without a schema change; canopy-web keeps them off (`KB_VECTORS`).
