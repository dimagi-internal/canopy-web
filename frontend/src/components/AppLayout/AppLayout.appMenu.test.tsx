// @vitest-environment jsdom
// What installed apps add to the account menu: an item that asks for an agent opens on a
// tap (a phone has no hover) and lists its agents, each a link to the item's page; the
// person chooses which agents it lists; and what was loaded shows at once on the next open.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { LocalAppMenu, LocalAppMenuAgent } from '@/api/localApps'

const getLocalAppMenu = vi.fn<() => Promise<LocalAppMenu>>()
const getLocalAppMenuAgents = vi.fn<() => Promise<LocalAppMenuAgent[]>>()
const setLocalAppMenuAgents = vi.fn<(app: string, item: string, agents: { workspace: string; slug: string }[]) => Promise<LocalAppMenu>>()
vi.mock('@/api/localApps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/localApps')>()),
  getLocalAppMenu,
  getLocalAppMenuAgents,
  setLocalAppMenuAgents,
}))

const { AppMenuItems } = await import('./AppLayout')
const { resetAppMenuForTests } = await import('./appMenuStore')

const HAL = { slug: 'hal', name: 'Hal', workspace: 'jonathan' }
const ACE = { slug: 'ace', name: 'ACE', workspace: 'dimagi' }
const VOICE = {
  app: 'meeting-assistant', app_name: 'Meeting assistant', key: 'voice-session', label: 'Voice session',
  path: '/listen/voice', pick: 'agent', agents: [HAL, ACE], chosen: false,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  resetAppMenuForTests()
})

const draw = () => render(<MemoryRouter><AppMenuItems onNavigate={() => undefined} /></MemoryRouter>)

describe('AppMenuItems', () => {
  it('opens an agent-picking item on a tap and links each agent to its page', async () => {
    getLocalAppMenu.mockResolvedValue({ items: [VOICE] })
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

  it('shows what it loaded before at once when the menu opens again', async () => {
    getLocalAppMenu.mockResolvedValue({ items: [VOICE] })
    draw()
    await screen.findByRole('button', { name: 'Voice session' })
    cleanup()
    getLocalAppMenu.mockReturnValue(new Promise(() => undefined)) // the server is slow this time
    draw()
    expect(screen.getByRole('button', { name: 'Voice session' })).toBeTruthy()
  })

  it('lets the person choose which agents an item lists, in their order', async () => {
    getLocalAppMenu.mockResolvedValue({ items: [VOICE] })
    getLocalAppMenuAgents.mockResolvedValue([ACE, HAL, { slug: 'eva', name: 'Eva', workspace: 'dimagi' }])
    setLocalAppMenuAgents.mockResolvedValue({ items: [{ ...VOICE, agents: [HAL], chosen: true }] })
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'Voice session' }))
    fireEvent.click(screen.getByRole('button', { name: 'Choose agents…' }))
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Hal' }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })) })
    expect(setLocalAppMenuAgents).toHaveBeenCalledWith('meeting-assistant', 'voice-session', [{ workspace: 'jonathan', slug: 'hal' }])
    expect(screen.getByRole('link', { name: 'Hal' })).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'ACE' })).toBeNull()
  })

  it('adds nothing when no app has turned an item on, or the menu fails to load', async () => {
    getLocalAppMenu.mockResolvedValue({ items: [] })
    const { container } = draw()
    await vi.waitFor(() => expect(getLocalAppMenu).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
    cleanup()
    resetAppMenuForTests()
    getLocalAppMenu.mockRejectedValue(new Error('offline'))
    const failed = draw()
    await vi.waitFor(() => expect(getLocalAppMenu).toHaveBeenCalledTimes(2))
    expect(failed.container.innerHTML).toBe('')
  })

  it('links an item that needs no agent straight to its page', async () => {
    getLocalAppMenu.mockResolvedValue({ items: [{ ...VOICE, key: 'notes', label: 'My notes', path: '/listen', pick: '', agents: [] }] })
    draw()
    expect((await screen.findByRole('link', { name: 'My notes' })).getAttribute('href')).toBe('/listen')
  })
})
