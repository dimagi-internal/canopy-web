/**
 * Turning a host's page snapshot into something an agent can read.
 *
 * The host hands over arbitrary JSON (`provideContext`). The agent reads prose.
 * This is the seam between them, and it is deliberately dumb: fence the JSON,
 * label it, and say where it came from. Anything cleverer — summarising,
 * reshaping, picking fields — would be canopy guessing at a host's meaning,
 * and the host is the only party that knows what its own state means.
 */

/** Above this, the snapshot is more likely to bury the user's question than to
 *  help answer it. A host that needs to send more should send less. */
const MAX_CHARS = 8000

export function buildContextPreamble(context: Record<string, unknown>): string | null {
  if (!context || Object.keys(context).length === 0) return null

  let body: string
  try {
    body = JSON.stringify(context, null, 2)
  } catch {
    // A host can hand us something with a cycle in it (a React fiber, a DOM
    // node). Losing the context is the right failure — the alternative is
    // throwing inside the send path and losing the user's message too.
    return null
  }

  const truncated = body.length > MAX_CHARS
  if (truncated) {
    // Cut rather than drop: the first fields are usually the identifying ones,
    // and a marked truncation is honest about what the agent is missing.
    body = `${body.slice(0, MAX_CHARS)}\n… truncated (${body.length} chars total)`
  }

  return [
    'Context from the page I am on:',
    '',
    '```json',
    body,
    '```',
  ].join('\n')
}
