// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

const mail = vi.hoisted(() => ({ getAgentMailbox: vi.fn(), setAgentMailbox: vi.fn() }))
vi.mock('@/api/mail', () => mail)

const { AgentMailboxControl } = await import('./AgentMailboxControl')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentMailboxControl', () => {
  it('shows a live canopy-hosted address as needing nothing', async () => {
    mail.getAgentMailbox.mockResolvedValue({ kind: 'hosted', address: 'scout@marshellis.com', live: true,
      verified: true, route: 'mine', domain: 'marshellis.com', hosted_domains: ['marshellis.com'], can_route: true })
    render(<AgentMailboxControl slug="scout" workspace="marshellis" canEdit />)
    expect((await screen.findByTestId('mailbox-hosted-live')).textContent).toMatch(/nothing to sign in/)
  })

  it('adds a hosted address in one step', async () => {
    mail.getAgentMailbox.mockResolvedValue({ kind: 'none', hosted_domains: ['marshellis.com'], can_route: true })
    mail.setAgentMailbox.mockResolvedValue({ kind: 'hosted', address: 'scout@marshellis.com', live: true,
      verified: true, route: 'mine', domain: 'marshellis.com', hosted_domains: ['marshellis.com'], can_route: true })
    render(<AgentMailboxControl slug="scout" workspace="marshellis" canEdit />)
    fireEvent.click(await screen.findByTestId('agent-mailbox-edit'))
    expect(screen.getByText(/Canopy-hosted here: @marshellis.com/)).toBeTruthy()
    fireEvent.change(screen.getByTestId('agent-mailbox-input'), { target: { value: 'scout@marshellis.com' } })
    fireEvent.click(screen.getByTestId('agent-mailbox-save'))
    expect(await screen.findByTestId('mailbox-hosted-live')).toBeTruthy()
    expect(mail.setAgentMailbox).toHaveBeenCalledWith('scout', 'scout@marshellis.com')
  })

  it('points a Google address at Credentials', async () => {
    mail.getAgentMailbox.mockResolvedValue({ kind: 'google', address: 'scout@gmail.com', hosted_domains: [] })
    render(<AgentMailboxControl slug="scout" workspace="marshellis" canEdit={false} />)
    expect(await screen.findByText(/Google mailbox/)).toBeTruthy()
    expect(screen.queryByTestId('agent-mailbox-edit')).toBeNull()
  })
})
