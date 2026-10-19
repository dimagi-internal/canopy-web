# canopy-kb

A knowledge base is everything one publisher has made public, kept as dated, cited
copies with the tables you compute with alongside. A curator agent writes a bundle
(`manifest.json`, `items.jsonl`, `tables/*.jsonl`); `build` turns it into `kb.sqlite`
(external-content FTS5, a dated change history); `KB` queries it; `tools` describes it
as MCP tools. canopy-web serves those tools per workspace project at
`/api/mcp/projects/<workspace>/<handle>/`. Contract: canopy `docs/knowledge-bases.md`.
