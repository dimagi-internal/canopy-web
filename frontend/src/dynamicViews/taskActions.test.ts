import { describe, expect, it, vi } from 'vitest'
import { TASK_NEEDS_CLICK, checkTaskAction, runTaskAction, type TaskActionContext } from './taskActions'

const VIEW: TaskActionContext = { viewId: 7, declared: ['done'], app: false }

describe('checkTaskAction', () => {
  it('passes a declared action from a click', () => {
    expect(checkTaskAction(VIEW, { key: ' CON-12 ', action: 'done' }, true)).toEqual({ ok: true, viewId: 7, key: 'CON-12', action: 'done' })
  })

  it('refuses an undeclared action, and anything that is not a status move', () => {
    for (const action of ['reopen', 'approve', 'delete']) {
      const r = checkTaskAction(VIEW, { key: 'CON-12', action }, true)
      expect(r).toEqual({ ok: false, error: `This view does not declare the task action "${action}" (its task_actions).` })
    }
    expect(checkTaskAction({ ...VIEW, declared: ['done', 'approve'] }, { key: 'CON-12', action: 'approve' }, true).ok).toBe(false)
  })

  it('refuses without a click, in plain words', () => {
    expect(checkTaskAction(VIEW, { key: 'CON-12', action: 'done' }, false)).toEqual({ ok: false, error: TASK_NEEDS_CLICK })
  })

  it('refuses an unsaved preview and an app page', () => {
    expect(checkTaskAction({ ...VIEW, viewId: null }, { key: 'CON-12', action: 'done' }, true))
      .toEqual({ ok: false, error: 'Save this view to change tasks from it.' })
    expect(checkTaskAction({ ...VIEW, app: true }, { key: 'CON-12', action: 'done' }, true).ok).toBe(false)
  })

  it('refuses a malformed request', () => {
    for (const req of [null, 'done', [], { key: '', action: 'done' }, { key: 12, action: 'done' }, { key: 'CON-12' }]) {
      expect(checkTaskAction(VIEW, req, true).ok).toBe(false)
    }
  })
})

describe('runTaskAction', () => {
  it('sends a checked action as the view, and nothing else', async () => {
    const client = vi.fn(async () => ({ key: 'CON-12', status: 'done' }))
    await expect(runTaskAction(client, VIEW, { key: 'CON-12', action: 'done' }, true)).resolves.toEqual({ key: 'CON-12', status: 'done' })
    expect(client).toHaveBeenCalledWith(7, 'CON-12', 'done')
    await expect(runTaskAction(client, VIEW, { key: 'CON-12', action: 'done' }, false)).rejects.toThrow(TASK_NEEDS_CLICK)
    await expect(runTaskAction(client, VIEW, { key: 'CON-12', action: 'reopen' }, true)).rejects.toThrow('does not declare')
    expect(client).toHaveBeenCalledTimes(1)
  })

  it("passes the server's refusal through as the rejection", async () => {
    const client = vi.fn(async () => {
      throw new Error("no task CON-99 in this view's project")
    })
    await expect(runTaskAction(client, VIEW, { key: 'CON-99', action: 'done' }, true)).rejects.toThrow("no task CON-99 in this view's project")
  })
})
