/**
 * The one thing a dynamic view may make happen: start an agent task it declares.
 *
 * A view's code calls `canopy.runAgentTask({agent, name, input, output_schema, …})`;
 * the frame posts it here and the canopy page starts the task
 * (`POST /api/w/<the view's workspace>/agent-tasks/`, docs/architecture/agent-tasks.md)
 * with the VIEWER's own session, then long-polls it until it is done, failed or
 * cancelled. So:
 *
 * - `checkAgentTask` refuses any `{agent, name}` the view does not declare
 *   (`agent_tasks` on the view, checked against its author when it was saved), and any
 *   field a view has no business setting — before anything is sent;
 * - the server's own gate then decides for the viewer: a viewer who may not give that
 *   agent a task gets the server's refusal, as a rejected promise with its message;
 * - `ViewAgentTasks.cancelAll()` cancels every task still running when the view goes
 *   away (the page unmounts the frame), and a view may re-read only tasks it started.
 *
 * Kept free of React and of the network (the client is passed in) so the rules are
 * tested directly (agentTasks.test.ts).
 */
import type { AgentTask, AgentTaskIn } from '@/api/agentTasks'

export interface DeclaredTask {
  agent: string
  name: string
}

export interface AgentTaskClient {
  create(body: AgentTaskIn): Promise<AgentTask>
  get(id: string, waitS: number, signal?: AbortSignal): Promise<AgentTask>
  cancel(id: string): Promise<AgentTask>
}

const FIELDS = new Set(['agent', 'name', 'input', 'output_schema', 'instructions', 'timeout_s', 'routing'])
const ROUTINGS = new Set(['prefer_local', 'local_only', 'any'])
const OPEN = new Set(['queued', 'running'])
/** The long-poll the server allows at most (`wait_s` 0–20). */
export const WAIT_S = 20

export type Checked = { ok: true; body: AgentTaskIn } | { ok: false; error: string }

export function checkAgentTask(declared: readonly DeclaredTask[], req: unknown): Checked {
  if (!req || typeof req !== 'object' || Array.isArray(req)) return { ok: false, error: 'runAgentTask takes an object' }
  const r = req as Record<string, unknown>
  const extra = Object.keys(r).filter((k) => !FIELDS.has(k))
  if (extra.length) return { ok: false, error: `runAgentTask does not take ${extra.join(', ')}` }
  const { agent, name } = r
  if (typeof agent !== 'string' || typeof name !== 'string') return { ok: false, error: 'runAgentTask needs agent and name' }
  if (!(declared ?? []).some((d) => d.agent === agent && d.name === name)) {
    return { ok: false, error: `this view does not declare the agent task ${agent} ${name} (its agent_tasks)` }
  }
  const schema = r.output_schema
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) {
    return { ok: false, error: 'output_schema must be a JSON Schema object' }
  }
  if (r.instructions != null && typeof r.instructions !== 'string') return { ok: false, error: 'instructions must be a string' }
  if (r.timeout_s != null && !Number.isInteger(r.timeout_s)) return { ok: false, error: 'timeout_s must be a whole number of seconds' }
  if (r.routing != null && !ROUTINGS.has(r.routing as string)) return { ok: false, error: `routing must be one of ${[...ROUTINGS].join(', ')}` }
  return {
    ok: true,
    body: {
      agent,
      name,
      input: r.input === undefined ? null : r.input,
      output_schema: schema as Record<string, unknown>,
      instructions: (r.instructions as string | undefined) ?? '',
      timeout_s: (r.timeout_s as number | undefined) ?? 900,
      routing: (r.routing as AgentTaskIn['routing'] | undefined) ?? 'prefer_local',
    },
  }
}

const aborted = () => new Error('the view was closed, so its agent task was cancelled')

/**
 * Start one task and wait for it: resolves with its `result` once `done`, rejects
 * with its `error` once failed or cancelled (the server fails a task at its
 * deadline). Aborting `signal` cancels the task.
 */
export async function runAgentTask(
  client: AgentTaskClient,
  body: AgentTaskIn,
  opts: { onStatus?: (task: AgentTask) => void; signal?: AbortSignal; maxErrors?: number } = {},
): Promise<unknown> {
  const { onStatus, signal, maxErrors = 3 } = opts
  if (signal?.aborted) throw aborted()
  let task = await client.create(body)
  let cancelled = false
  const cancel = () => {
    if (cancelled || !OPEN.has(task.status)) return
    cancelled = true
    client.cancel(task.id).catch(() => undefined)
  }
  signal?.addEventListener('abort', cancel, { once: true })
  try {
    onStatus?.(task)
    let errors = 0
    while (OPEN.has(task.status)) {
      if (signal?.aborted) {
        cancel()
        throw aborted()
      }
      let next: AgentTask
      try {
        next = await client.get(task.id, WAIT_S, signal)
        errors = 0
      } catch (e) {
        if (signal?.aborted) {
          cancel()
          throw aborted()
        }
        // A dropped long-poll is retried; a refusal that keeps coming is the answer.
        if (++errors >= maxErrors) throw e
        continue
      }
      if (next.status !== task.status) onStatus?.(next)
      task = next
    }
    if (task.status === 'done') return task.result ?? null
    throw new Error(task.error || `the agent task was ${task.status}`)
  } finally {
    signal?.removeEventListener('abort', cancel)
  }
}

/** One frame's agent tasks: what it declared, what it started, and cancelling them. */
export class ViewAgentTasks {
  private running = new Set<AbortController>()
  private started = new Set<string>()
  private client: AgentTaskClient
  private declared: () => readonly DeclaredTask[]

  constructor(client: AgentTaskClient, declared: () => readonly DeclaredTask[]) {
    this.client = client
    this.declared = declared
  }

  run(req: unknown, onStatus?: (task: AgentTask) => void): Promise<unknown> {
    const checked = checkAgentTask(this.declared(), req)
    if (!checked.ok) return Promise.reject(new Error(checked.error))
    const ctl = new AbortController()
    this.running.add(ctl)
    return runAgentTask(this.client, checked.body, {
      signal: ctl.signal,
      onStatus: (t) => {
        this.started.add(t.id)
        onStatus?.(t)
      },
    }).finally(() => this.running.delete(ctl))
  }

  /** A task this frame started; any other id is refused, so a view reads only its own. */
  read(id: unknown): Promise<AgentTask> {
    if (typeof id !== 'string' || !this.started.has(id)) {
      return Promise.reject(new Error('this view did not start that agent task'))
    }
    return this.client.get(id, 0)
  }

  /** The view is going away: cancel every task it is still waiting on. */
  cancelAll(): void {
    for (const ctl of this.running) ctl.abort()
    this.running.clear()
  }
}
