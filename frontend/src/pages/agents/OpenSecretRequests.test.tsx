// @vitest-environment jsdom
// What an agent asked for and nobody answered, on its Credentials page.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const vault = vi.hoisted(() => ({ listSecretRequests: vi.fn() }))
vi.mock('@/api/vault', () => vault)
vi.mock('@/components/vault/SecretSealCard', () => ({
  SecretSealCard: ({ name, requestId }: { name: string; requestId?: string }) =>
    <div data-testid="seal-card">{name}:{requestId}</div>,
}))

const { OpenSecretRequests } = await import('./OpenSecretRequests')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const page = () => render(
  <MemoryRouter initialEntries={['/w/family/agents/bailey/settings']}>
    <Routes><Route path="/w/:workspace/*" element={<OpenSecretRequests slug="bailey" />} /></Routes>
  </MemoryRouter>)

const req = (id: string, name: string, session_id: string | null) => ({
  id, secret_request_id: id, agent: 'bailey', name, reason: '', status: 'open', has_value: false,
  session_id, created_at: '2026-10-24T00:00:00Z', expires_at: '2026-10-25T00:00:00Z',
})

describe('OpenSecretRequests', () => {
  it('shows a seal card per open request, saying where each was asked', async () => {
    vault.listSecretRequests.mockResolvedValue([req('r1', 'kindora', null), req('r2', 'GITHUB_TOKEN', 's1')])
    page()
    expect((await screen.findAllByTestId('seal-card')).map((c) => c.textContent)).toEqual(['kindora:r1', 'GITHUB_TOKEN:r2'])
    expect(screen.getByText('Asked outside any of its chats')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'a chat' }).getAttribute('href')).toBe('/w/family/chat/s1')
  })

  it('shows nothing when nothing is waiting', async () => {
    vault.listSecretRequests.mockResolvedValue([])
    page()
    await Promise.resolve()
    expect(screen.queryByTestId('open-secret-requests')).toBeNull()
  })
})
