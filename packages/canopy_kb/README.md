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
`grep` (a figure or quote in a long document) and `sql` (the declared tables).

| module | what |
|---|---|
| `chunking.py` | passages (3,200-char spans with a heading path, 400 overlap) and outlines |
| `entities.py` | the entity index from catalog cards, merged by key, acronym and alias |
| `kb.py` | `build` (schema 3) and `KB`: passage search for the vector sync and `kb_eval` |
| `research.py` | the research tools, as text |
| `hybrid.py` | what a passage's vector is made of, and fusion |
| `tools.py` | MCP tool specs and dispatch |
