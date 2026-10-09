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
    await waitFor(() => expect(screen.getByText('Learn: on')).toBeTruthy())
    expect(screen.queryByText(/Use:/)).toBeNull()
  })

  it('turning on what is off by default sets an override; turning it back inherits', async () => {
    getSessionMemory.mockResolvedValue({ session_id: 's1', record: feature(true, false), use: feature(true, false) })
    setSessionMemory.mockResolvedValueOnce({ session_id: 's1', record: feature(true, false), use: feature(true, false, true) })
    render(<SessionMemoryToggles sessionId="s1" />)
    await waitFor(() => expect(screen.getByText('Use: off')).toBeTruthy())
    fireEvent.click(screen.getByText('Use: off'))
    await waitFor(() => expect(screen.getByText('Use: on')).toBeTruthy())
    expect(setSessionMemory).toHaveBeenLastCalledWith('s1', { use: 'on' })
    setSessionMemory.mockResolvedValueOnce({ session_id: 's1', record: feature(true, false), use: feature(true, false) })
    fireEvent.click(screen.getByText('Use: on'))
    await waitFor(() => expect(screen.getByText('Use: off')).toBeTruthy())
    expect(setSessionMemory).toHaveBeenLastCalledWith('s1', { use: 'inherit' })
  })

  it('a read-only source shows the state as text with a link to change it in canopy', async () => {
    const save = vi.fn()
    const source = {
      load: vi.fn().mockResolvedValue({
        state: { session_id: 's1', record: feature(true, true), use: feature(true, false) },
        editable: false,
        manageUrl: 'https://canopy.example/w/connect/chat/s1',
      }),
      save,
      grant: vi.fn(),
    }
    render(<SessionMemoryToggles sessionId="s1" source={source} />)
    await waitFor(() => expect(screen.getByText('Learn: on')).toBeTruthy())
    expect(screen.queryByRole('switch')).toBeNull()
    fireEvent.click(screen.getByText('Learn: on'))
    expect(save).not.toHaveBeenCalled()
    const link = screen.getByText('Change in canopy') as HTMLAnchorElement
    expect(link.href).toBe('https://canopy.example/w/connect/chat/s1')
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
    await waitFor(() => expect(screen.getByText('Learn: on')).toBeTruthy())
    fireEvent.click(screen.getByRole('switch'))
    await waitFor(() => expect(screen.getByText('Learn: off')).toBeTruthy())
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

  it('asks the person — agent, categories, actions, duration — before anything is granted', async () => {
    getSessionMemory.mockResolvedValue({
      session_id: 's1', agent: ADA, categories: CATS, session_grant_hours: 24,
      record: feature(true, true, null, false), use: feature(false, false),
    })
    render(<SessionMemoryToggles sessionId="s1" />)
    const dialog = await screen.findByRole('dialog', { name: 'Grant Ada access' })
    expect(dialog.textContent).toContain('save what it learns about you')
    expect(dialog.textContent).toContain('work context, general preferences · write · for this session only')
    expect(screen.getByText('Learn: on, not granted')).toBeTruthy()
    expect(grantSessionAgent).not.toHaveBeenCalled() // nothing preselected or sent
  })

  it('allowing is for this session; keeping it is a separate act asked afterwards', async () => {
    getSessionMemory.mockResolvedValue({
      session_id: 's1', agent: ADA, categories: CATS,
      record: feature(true, true, null, false), use: feature(false, false),
    })
    grantSessionAgent.mockResolvedValue({
      session_id: 's1', agent: ADA, categories: CATS,
      record: feature(true, true, null, true), use: feature(false, false),
    })
    render(<SessionMemoryToggles sessionId="s1" />)
    await screen.findByRole('dialog', { name: 'Grant Ada access' })
    expect(screen.queryByText(/Keep allowing/)).toBeNull() // never in the same act
    fireEvent.click(screen.getByText('Allow for this session'))
    await waitFor(() => expect(grantSessionAgent).toHaveBeenCalledWith('s1', ['record'], 'session', 'chat'))
    const keep = await screen.findByRole('dialog', { name: 'Keep allowing Ada' })
    expect(keep.textContent).toContain('until you revoke it') // what persistent means, first
    fireEvent.click(screen.getByText('Keep allowing Ada'))
    await waitFor(() => expect(grantSessionAgent).toHaveBeenLastCalledWith('s1', ['record'], 'always', 'chat'))
  })

  it('"Just this session" and "Not now" grant nothing more', async () => {
    getSessionMemory.mockResolvedValue({
      session_id: 's1', agent: ADA, categories: CATS,
      record: feature(true, true, null, false), use: feature(true, true, null, false),
    })
    grantSessionAgent.mockResolvedValue({
      session_id: 's1', agent: ADA, categories: CATS,
      record: feature(true, true), use: feature(true, true),
    })
    const { unmount } = render(<SessionMemoryToggles sessionId="s1" />)
    fireEvent.click(await screen.findByText('Allow for this session'))
    fireEvent.click(await screen.findByText('Just this session'))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(grantSessionAgent).toHaveBeenCalledTimes(1)
    expect(grantSessionAgent).toHaveBeenCalledWith('s1', ['record', 'use'], 'session', 'chat')
    unmount()
    grantSessionAgent.mockClear()
    render(<SessionMemoryToggles sessionId="s1" />)
    fireEvent.click(await screen.findByText('Not now'))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(grantSessionAgent).not.toHaveBeenCalled()
  })

  it('a read-only viewer is told it is not granted and sent to canopy, never asked', async () => {
    const source = {
      load: vi.fn().mockResolvedValue({
        state: { session_id: 's1', agent: ADA, categories: CATS, record: feature(true, true, null, false), use: feature(false, false) },
        editable: false,
        manageUrl: 'https://canopy.example/w/connect/chat/s1',
      }),
      save: vi.fn(),
      grant: vi.fn(),
    }
    render(<SessionMemoryToggles sessionId="s1" source={source} />)
    await screen.findByText(/Not granted to Ada yet/)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect((screen.getByText('grant it in canopy') as HTMLAnchorElement).href).toBe('https://canopy.example/w/connect/chat/s1')
    expect(source.grant).not.toHaveBeenCalled()
  })
})
