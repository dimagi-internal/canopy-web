import { describe, expect, it, vi } from 'vitest'

import { embedMemorySource } from './sessionMemory'

const feature = { available: true, default: true, override: null, effective: true }
const state = { session_id: 's1', record: feature, use: { ...feature, default: false, effective: false } }
const err = (status: number) => Object.assign(new Error(`canopy request failed (${status})`), { status })

describe('embedMemorySource', () => {
  it("canopy's own widget: the person route answers, so the toggles are editable", async () => {
    const json = vi.fn().mockResolvedValue(state)
    const loaded = await embedMemorySource({ json }, 's1', 'https://c').load()
    expect(loaded).toEqual({ state, editable: true })
    expect(json).toHaveBeenCalledWith('/api/people/me/sessions/s1/agent-memory/')
  })

  it('a connected site: the person route is refused, so it shows read-only with a link', async () => {
    const json = vi
      .fn()
      .mockRejectedValueOnce(err(403))
      .mockResolvedValueOnce({ ...state, manage_path: '/w/connect/chat/s1' })
    const loaded = await embedMemorySource({ json }, 's1', 'https://c/canopy').load()
    expect(loaded).toEqual({ state, editable: false, manageUrl: 'https://c/canopy/w/connect/chat/s1' })
    expect(json).toHaveBeenLastCalledWith('/api/embed/sessions/s1/agent-memory')
  })

  it('not your session (404) hides it without asking the embed route', async () => {
    const json = vi.fn().mockRejectedValue(err(404))
    expect(await embedMemorySource({ json }, 's1', 'https://c').load()).toBeNull()
    expect(json).toHaveBeenCalledTimes(1)
  })

  it('both refused hides it', async () => {
    const json = vi.fn().mockRejectedValueOnce(err(403)).mockRejectedValueOnce(err(404))
    expect(await embedMemorySource({ json }, 's1', 'https://c').load()).toBeNull()
  })

  it('saves through the person route only', async () => {
    const json = vi.fn().mockResolvedValue(state)
    await embedMemorySource({ json }, 's1', 'https://c').save({ use: 'on' })
    expect(json).toHaveBeenCalledWith('/api/people/me/sessions/s1/agent-memory/', {
      method: 'PUT',
      body: JSON.stringify({ use: 'on' }),
    })
  })

  it('grants through the person route only, marked as given in the widget', async () => {
    const json = vi.fn().mockResolvedValue(state)
    await embedMemorySource({ json }, 's1', 'https://c').grant(['record'], 'session')
    expect(json).toHaveBeenCalledWith('/api/people/me/sessions/s1/agent-grants/', {
      method: 'POST',
      body: JSON.stringify({ features: ['record'], duration: 'session', surface: 'widget' }),
    })
  })
})
