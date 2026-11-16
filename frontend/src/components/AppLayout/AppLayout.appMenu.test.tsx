// @vitest-environment jsdom
// What installed apps add to the account menu: an item that asks for an agent opens on a
// tap (a phone has no hover) and lists the agents, each a link to the item's page.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { LocalAppMenu } from '@/api/localApps'

const getLocalAppMenu = vi.fn<() => Promise<LocalAppMenu>>()
vi.mock('@/api/localApps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/localApps')>()),
  getLocalAppMenu,
}))

const { AppMenuItems } = await import('./AppLayout')

const VOICE = { app: 'meeting-assistant', app_name: 'Meeting assistant', key: 'voice-session', label: 'Voice session', path: '/listen/voice', pick: 'agent' }

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AppMenuItems', () => {
  it('opens an agent-picking item on a tap and links each agent to its page', async () => {
    getLocalAppMenu.mockResolvedValue({
      items: [VOICE],
      agents: [{ slug: 'hal', name: 'Hal', workspace: 'jonathan' }, { slug: 'ace', name: 'ACE', workspace: 'dimagi' }],
    })
    const onNavigate = vi.fn()
    render(<MemoryRouter><AppMenuItems onNavigate={onNavigate} /></MemoryRouter>)
    const item = await screen.findByRole('button', { name: 'Voice session' })
    expect(item.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('link', { name: 'Hal' })).toBeNull()
    fireEvent.click(item)
    const hal = screen.getByRole('link', { name: 'Hal' })
    expect(hal.getAttribute('href')).toBe('/listen/voice?agent=hal&workspace=jonathan')
    expect(screen.getByRole('link', { name: 'ACE' }).getAttribute('href')).toBe('/listen/voice?agent=ace&workspace=dimagi')
    fireEvent.click(hal)
    expect(onNavigate).toHaveBeenCalledTimes(1)
  })

  it('adds nothing when no app has turned an item on, or the menu fails to load', async () => {
    getLocalAppMenu.mockResolvedValue({ items: [], agents: [] })
    const { container } = render(<MemoryRouter><AppMenuItems onNavigate={() => undefined} /></MemoryRouter>)
    await vi.waitFor(() => expect(getLocalAppMenu).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
    cleanup()
    getLocalAppMenu.mockRejectedValue(new Error('offline'))
    const failed = render(<MemoryRouter><AppMenuItems onNavigate={() => undefined} /></MemoryRouter>)
    await vi.waitFor(() => expect(getLocalAppMenu).toHaveBeenCalledTimes(2))
    expect(failed.container.innerHTML).toBe('')
  })

  it('links an item that needs no agent straight to its page', async () => {
    getLocalAppMenu.mockResolvedValue({ items: [{ ...VOICE, key: 'notes', label: 'My notes', path: '/listen', pick: '' }], agents: [] })
    render(<MemoryRouter><AppMenuItems onNavigate={() => undefined} /></MemoryRouter>)
    expect((await screen.findByRole('link', { name: 'My notes' })).getAttribute('href')).toBe('/listen')
  })
})
