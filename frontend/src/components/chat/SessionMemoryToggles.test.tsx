// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const getSessionMemory = vi.fn()
const setSessionMemory = vi.fn()
const grantSessionAgent = vi.fn()
vi.mock('@/api/people', () => ({
  getSessionMemory: (...a: unknown[]) => getSessionMemory(...a),
  setSessionMemory: (...a: unknown[]) => setSessionMemory(...a),
  grantSessionAgent: (...a: unknown[]) => grantSessionAgent(...a),
}))

import { SessionMemoryToggles } from './SessionMemoryToggles'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const feature = (available: boolean, def: boolean, override: boolean | null = null, granted = true) => ({
  available,
  default: def,
  override,
  effective: available && (override ?? def),
  granted,
  grant: null,
})

const ADA = { slug: 'ada', name: 'Ada' }

/** The chip, found by what its dots say (green on / red off). */
const chip = (summary: string) =>
  screen.findByRole('button', { name: `Your memory in this conversation: ${summary}` })
const CATS = ['work_context', 'general_preferences']

describe('SessionMemoryToggles', () => {
  it('renders nothing when the session is not yours', async () => {
    getSessionMemory.mockRejectedValue(new Error('404'))
    const { container } = render(<SessionMemoryToggles sessionId="s1" />)
    await waitFor(() => expect(getSessionMemory).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('shows only available features, with their effective state', async () => {
    getSessionMemory.mockResolvedValue({ session_id: 's1', record: feature(true, true), use: feature(false, false) })
    render(<SessionMemoryToggles sessionId="s1" />)
    await chip('Learn on')
    expect(screen.queryByText(/Use/)).toBeNull()
  })

  it('renders nothing when memory is not on for this person (rollout gate)', async () => {
    getSessionMemory.mockResolvedValue({
      session_id: 's1', record: feature(true, true), use: feature(true, true), memory_enabled: false,
    })
    const { container } = render(<SessionMemoryToggles sessionId="s1" />)
    await waitFor(() => expect(getSessionMemory).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(container.textContent).toBe('')
  })

  it('turning on what is off by default sets an override; turning it back inherits', async () => {
    getSessionMemory.mockResolvedValue({ session_id: 's1', record: feature(true, false), use: feature(true, false) })
    setSessionMemory.mockResolvedValueOnce({ session_id: 's1', record: feature(true, false), use: feature(true, false, true) })
    render(<SessionMemoryToggles sessionId="s1" />)
    // One compact chip; clicking it opens the panel with a switch per feature.
    fireEvent.click(await chip('Learn off, Use off'))
    const use = () => screen.getByRole('switch', { name: 'Use my memory in this conversation' })
    expect(use().getAttribute('aria-checked')).toBe('false')
    fireEvent.click(use())
    await chip('Learn off, Use on')
    expect(use().getAttribute('aria-checked')).toBe('true')
    expect(setSessionMemory).toHaveBeenLastCalledWith('s1', { use: 'on' })
    setSessionMemory.mockResolvedValueOnce({ session_id: 's1', record: feature(true, false), use: feature(true, false) })
    fireEvent.click(use())
    await chip('Learn off, Use off')
    expect(setSessionMemory).toHaveBeenLastCalledWith('s1', { use: 'inherit' })
  })

  it('a read-only source shows the state as text with a link to change it in canopy', async () => {
    const save = vi.fn()
    const source = {
      load: vi.fn().mockResolvedValue({
        state: { session_id: 's1', record: feature(true, true), use: feature(true, false) },
        editable: false,
        manageUrl: 'https://canopy.example/w/connect/sessions/s1',
      }),
      save,
      grant: vi.fn(),
    }
    render(<SessionMemoryToggles sessionId="s1" source={source} />)
    fireEvent.click(await chip('Learn on, Use off'))
    expect(screen.getByRole('dialog', { name: 'Your memory in this conversation' })).toBeTruthy()
    expect(screen.queryByRole('switch')).toBeNull()
    expect(save).not.toHaveBeenCalled()
    const link = screen.getByText('Change in canopy') as HTMLAnchorElement
    expect(link.href).toBe('https://canopy.example/w/connect/sessions/s1')
    expect(link.target).toBe('_blank')
  })

  it('an editable injected source is what reads and writes, not the app routes', async () => {
    const source = {
      load: vi.fn().mockResolvedValue({
        state: { session_id: 's1', record: feature(true, true), use: feature(false, false) },
        editable: true,
      }),
      save: vi.fn().mockResolvedValue({ session_id: 's1', record: feature(true, true, false), use: feature(false, false) }),
      grant: vi.fn(),
    }
    render(<SessionMemoryToggles sessionId="s1" source={source} />)
    fireEvent.click(await chip('Learn on'))
    fireEvent.click(screen.getByRole('switch'))
    await chip('Learn off')
    expect(source.save).toHaveBeenCalledWith({ record: 'off' })
    expect(getSessionMemory).not.toHaveBeenCalled()
    expect(setSessionMemory).not.toHaveBeenCalled()
  })

  it('a source that answers null hides everything', async () => {
    const source = { load: vi.fn().mockResolvedValue(null), save: vi.fn(), grant: vi.fn() }
    const { container } = render(<SessionMemoryToggles sessionId="s1" source={source} />)
    await waitFor(() => expect(source.load).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('an answer it does not recognise hides the toggles instead of crashing', async () => {
    const source = { load: vi.fn().mockResolvedValue({ state: { id: 'sess-1' }, editable: true }), save: vi.fn(), grant: vi.fn() }
    const { container } = render(<SessionMemoryToggles sessionId="s1" source={source as never} />)
    await waitFor(() => expect(source.load).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('never pops up to ask: an ungranted agent just reads as off (red), no "?"', async () => {
    getSessionMemory.mockResolvedValue({
      session_id: 's1', agent: ADA, categories: CATS, session_grant_hours: 24, org: 'Dimagi',
      record: feature(true, true, null, false), use: feature(true, true, null, false),
    })
    render(<SessionMemoryToggles sessionId="s1" />)
    await chip('Learn off, Use off')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.body.textContent).not.toContain('?')
    expect(grantSessionAgent).not.toHaveBeenCalled()
  })

  it('turning an ungranted feature on allows this agent for the session in one click', async () => {
    getSessionMemory.mockResolvedValue({
      session_id: 's1', agent: ADA, categories: CATS,
      record: feature(true, true, null, false), use: feature(false, false),
    })
    grantSessionAgent.mockResolvedValueOnce({
      session_id: 's1', agent: ADA, categories: CATS,
      record: { ...feature(true, true), grant: { type: 'session' } }, use: feature(false, false),
    })
    render(<SessionMemoryToggles sessionId="s1" />)
    fireEvent.click(await chip('Learn off'))
    fireEvent.click(screen.getByRole('switch', { name: 'Learn from me in this conversation' }))
    await chip('Learn on')
    expect(grantSessionAgent).toHaveBeenCalledWith('s1', ['record'], 'session', 'chat')
    expect(setSessionMemory).not.toHaveBeenCalled() // already on by default — only the grant was missing
    // Keeping it is the person's separate act, offered in the panel, never popped up.
    grantSessionAgent.mockResolvedValueOnce({
      session_id: 's1', agent: ADA, categories: CATS,
      record: { ...feature(true, true), grant: { type: 'persistent' } }, use: feature(false, false),
    })
    fireEvent.click(screen.getByText('Allow Ada in every conversation'))
    await waitFor(() => expect(grantSessionAgent).toHaveBeenLastCalledWith('s1', ['record'], 'always', 'chat'))
  })

  it('turning on a feature that is off switches it on, then allows the agent', async () => {
    getSessionMemory.mockResolvedValue({
      session_id: 's1', agent: ADA, record: feature(true, false, null, false), use: feature(false, false),
    })
    setSessionMemory.mockResolvedValueOnce({
      session_id: 's1', agent: ADA, record: feature(true, false, true, false), use: feature(false, false),
    })
    grantSessionAgent.mockResolvedValueOnce({
      session_id: 's1', agent: ADA, record: feature(true, false, true, true), use: feature(false, false),
    })
    render(<SessionMemoryToggles sessionId="s1" />)
    fireEvent.click(await chip('Learn off'))
    fireEvent.click(screen.getByRole('switch'))
    await chip('Learn on')
    expect(setSessionMemory).toHaveBeenCalledWith('s1', { record: 'on' })
    expect(grantSessionAgent).toHaveBeenCalledWith('s1', ['record'], 'session', 'chat')
  })

  it('a read-only viewer sees it off and is sent to canopy, never asked', async () => {
    const source = {
      load: vi.fn().mockResolvedValue({
        state: { session_id: 's1', agent: ADA, categories: CATS, record: feature(true, true, null, false), use: feature(false, false) },
        editable: false,
        manageUrl: 'https://canopy.example/w/connect/sessions/s1',
      }),
      save: vi.fn(),
      grant: vi.fn(),
    }
    render(<SessionMemoryToggles sessionId="s1" source={source} />)
    fireEvent.click(await chip('Learn off'))
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.queryByText(/Allow/)).toBeNull()
    expect((screen.getByText('Change in canopy') as HTMLAnchorElement).href).toBe('https://canopy.example/w/connect/sessions/s1')
    expect(source.grant).not.toHaveBeenCalled()
  })
})
