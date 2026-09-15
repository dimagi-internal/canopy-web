/**
 * Turning a host's page snapshot into something an agent can read.
 *
 * It goes AFTER what the person typed, not before — which is why this is a
 * "block" and no longer a "preamble". Leading with it cost something concrete:
 * the runner names an emdash task from the prompt's opening words, so somebody
 * who typed "tell me about this page" got a task called
 * `c-context-from-the-page-i-am-on-8e56` and could not find their own
 * conversation. The title of a thing is how you find it again.
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

export function buildPageContextBlock(context: Record<string, unknown>): string | null {
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
