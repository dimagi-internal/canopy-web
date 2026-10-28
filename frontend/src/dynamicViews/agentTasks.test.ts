import { describe, expect, it, vi } from 'vitest'
import type { AgentTask } from '@/api/agentTasks'
import { ViewAgentTasks, checkAgentTask, type AgentTaskClient } from './agentTasks'

const DECLARED = [{ agent: 'bailey', name: 'gmail-triage.review' }, { agent: 'bailey', name: 'gmail-triage.apply' }]
const SCHEMA = { type: 'object', required: ['rows'], properties: { rows: { type: 'array' } } }
const REQ = { agent: 'bailey', name: 'gmail-triage.review', input: { count: 50 }, output_schema: SCHEMA }

function task(status: AgentTask['status'], extra: Partial<AgentTask> = {}): AgentTask {
  return {
    id: 't1', agent: 'bailey', name: 'gmail-triage.review', status, error: '', nudges: 0, timeout_s: 900,
    deadline_at: '2026-10-27T12:15:00Z', created_at: '2026-10-27T12:00:00Z', instructions: '',
    output_schema: SCHEMA, ...extra,
  } as AgentTask
}

/** A client whose long-poll answers each status in turn, then holds the last. */
function client(statuses: AgentTask[], over: Partial<AgentTaskClient> = {}) {
  let i = 0
  return {
    create: vi.fn(async () => task('queued')),
    get: vi.fn(async () => statuses[Math.min(i++, statuses.length - 1)]),
    cancel: vi.fn(async () => task('cancelled', { error: 'cancelled by its creator' })),
    ...over,
  }
}

describe('checkAgentTask', () => {
  it('passes a declared task through with the server defaults', () => {
    expect(checkAgentTask(DECLARED, REQ)).toEqual({
      ok: true,
      body: { ...REQ, instructions: '', timeout_s: 900, routing: 'prefer_local' },
    })
  })

  it('refuses a task the view does not declare, by agent or by name', () => {
    expect(checkAgentTask(DECLARED, { ...REQ, agent: 'hal' })).toEqual({
      ok: false, error: 'this view does not declare the agent task hal gmail-triage.review (its agent_tasks)',
    })
    expect(checkAgentTask(DECLARED, { ...REQ, name: 'gmail-triage.delete-all' }).ok).toBe(false)
    expect(checkAgentTask([], REQ).ok).toBe(false)
  })

  it('refuses fields a view has no business setting, and malformed ones', () => {
    expect(checkAgentTask(DECLARED, { ...REQ, parent: { turn: 'x' } })).toEqual({ ok: false, error: 'runAgentTask does not take parent' })
    expect(checkAgentTask(DECLARED, { ...REQ, output_schema: [] }).ok).toBe(false)
    expect(checkAgentTask(DECLARED, { ...REQ, routing: 'everywhere' }).ok).toBe(false)
    expect(checkAgentTask(DECLARED, { ...REQ, timeout_s: 1.5 }).ok).toBe(false)
    expect(checkAgentTask(DECLARED, 'bailey').ok).toBe(false)
  })
})

describe('ViewAgentTasks', () => {
  it('refuses an undeclared task without calling the server', async () => {
    const c = client([task('done')])
    const tasks = new ViewAgentTasks(c, () => DECLARED)
    await expect(tasks.run({ ...REQ, name: 'other' })).rejects.toThrow('does not declare')
    expect(c.create).not.toHaveBeenCalled()
  })

  it('resolves with the result once the task is done, reporting each status', async () => {
    const c = client([task('running'), task('running'), task('done', { result: { rows: [1, 2] } })])
    const seen: string[] = []
    const out = await new ViewAgentTasks(c, () => DECLARED).run(REQ, (t) => seen.push(t.status))
    expect(out).toEqual({ rows: [1, 2] })
    expect(seen).toEqual(['queued', 'running', 'done'])
    expect(c.create).toHaveBeenCalledWith({ ...REQ, instructions: '', timeout_s: 900, routing: 'prefer_local' })
    expect(c.get).toHaveBeenCalledWith('t1', 20, expect.anything())
  })

  it('rejects with the server’s reason when the task fails', async () => {
    const c = client([task('failed', { error: 'timed out after 900s without a result' })])
    await expect(new ViewAgentTasks(c, () => DECLARED).run(REQ)).rejects.toThrow('timed out after 900s without a result')
  })

  it('surfaces the server’s refusal of the viewer as a rejected promise', async () => {
    const refused = new Error('running a turn for this agent requires the editor or owner role')
    const c = client([], { create: vi.fn(async () => { throw refused }) })
    await expect(new ViewAgentTasks(c, () => DECLARED).run(REQ)).rejects.toBe(refused)
  })

  it('cancels a running task when the view goes away', async () => {
    const c = client([], {
      get: vi.fn((_id: string, _w: number, signal?: AbortSignal) => new Promise<AgentTask>((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(new Error('aborted')))
      })),
    })
    const tasks = new ViewAgentTasks(c, () => DECLARED)
    const running = tasks.run(REQ)
    await vi.waitFor(() => expect(c.get).toHaveBeenCalled())
    tasks.cancelAll()
    await expect(running).rejects.toThrow('cancelled')
    expect(c.cancel).toHaveBeenCalledWith('t1')
  })

  it('re-reads only tasks it started', async () => {
    const c = client([task('done', { result: { rows: [] } })])
    const tasks = new ViewAgentTasks(c, () => DECLARED)
    await expect(tasks.read('t1')).rejects.toThrow('did not start')
    await tasks.run(REQ)
    await expect(tasks.read('t1')).resolves.toMatchObject({ id: 't1', status: 'done' })
    await expect(tasks.read('someone-elses')).rejects.toThrow('did not start')
  })
})
