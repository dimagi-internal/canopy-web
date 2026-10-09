// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const getSessionMemory = vi.fn()
const setSessionMemory = vi.fn()
vi.mock('@/api/people', () => ({
  getSessionMemory: (...a: unknown[]) => getSessionMemory(...a),
  setSessionMemory: (...a: unknown[]) => setSessionMemory(...a),
}))

import { SessionMemoryToggles } from './SessionMemoryToggles'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const feature = (available: boolean, def: boolean, override: boolean | null = null) => ({
  available,
  default: def,
  override,
  effective: available && (override ?? def),
})

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
    const source = { load: vi.fn().mockResolvedValue(null), save: vi.fn() }
    const { container } = render(<SessionMemoryToggles sessionId="s1" source={source} />)
    await waitFor(() => expect(source.load).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('an answer it does not recognise hides the toggles instead of crashing', async () => {
    const source = { load: vi.fn().mockResolvedValue({ state: { id: 'sess-1' }, editable: true }), save: vi.fn() }
    const { container } = render(<SessionMemoryToggles sessionId="s1" source={source as never} />)
    await waitFor(() => expect(source.load).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })
})
