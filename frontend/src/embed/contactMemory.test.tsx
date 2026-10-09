// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(cleanup)

import { ContactMemory } from './ContactMemory'
import { contactHcp, contactMemorySource, type ContactHcpState, type MemoryRest } from './sessionMemory'

const feature = { available: true, default: true, override: null, effective: true, granted: true, grant: null }
const session = { session_id: 's1', record: feature, use: { ...feature, available: false, effective: false, granted: false } }
const base: ContactHcpState = {
  eligible: true, reason: '', opted_in: false, email: 'v@partner.org', site: 'connect-labs',
  categories: ['work_context', 'general_preferences'], session_grant_hours: 24, policy: {},
  session: null, grants: [],
}

/** A fake frame client: the proof call, then the HCP call it authorizes. */
function rest(reply: unknown) {
  const json = vi.fn(async (path: string) =>
    path === '/api/contact/hcp/proof' ? { proof: 'p-1' } : reply,
  )
  return { json: json as unknown as MemoryRest['json'] & typeof json }
}

describe('contactHcp', () => {
  it('fetches a fresh frame proof before every call and sends it', async () => {
    const r = rest(base)
    await contactHcp(r).optIn('s1', false)
    expect(r.json).toHaveBeenNthCalledWith(1, '/api/contact/hcp/proof', { method: 'POST' })
    expect(r.json).toHaveBeenNthCalledWith(2, '/api/contact/hcp/opt-in', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's1', use: false }),
      headers: { 'X-Canopy-Frame-Proof': 'p-1' },
    })
  })

  it('the toggles source is empty until the contact has opted in', async () => {
    expect(await contactMemorySource(contactHcp(rest(base)), 's1').load()).toBeNull()
    const opted = { ...base, opted_in: true, session }
    expect(await contactMemorySource(contactHcp(rest(opted)), 's1').load()).toEqual({
      state: session,
      editable: true,
    })
  })

  it('an ineligible contact gets nothing', async () => {
    const r = rest({ ...base, eligible: false, reason: 'untrusted_site' })
    expect(await contactMemorySource(contactHcp(r), 's1').load()).toBeNull()
  })
})

describe('ContactMemory', () => {
  it('renders nothing for an ineligible contact', async () => {
    const hcp = contactHcp(rest({ ...base, eligible: false }))
    const { container } = render(<ContactMemory hcp={hcp} sessionId="s1" agentName="Echo" />)
    await new Promise((r) => setTimeout(r, 0))
    expect(container.textContent).toBe('')
  })

  it('says what it is before the contact opts in, and use is unticked', async () => {
    const r = rest(base)
    render(<ContactMemory hcp={contactHcp(r)} sessionId="s1" agentName="Echo" />)
    fireEvent.click(await screen.findByText('Remember me?'))
    const dialog = screen.getByRole('dialog', { name: 'Let canopy learn about you' })
    expect(dialog.textContent).toContain('v@partner.org')
    expect(dialog.textContent).toContain('confirmed by connect-labs')
    expect(dialog.textContent).toContain('work context')
    expect(dialog.textContent).toContain('For this conversation only')
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(false)
    fireEvent.click(screen.getByText('Allow for this conversation'))
    await waitFor(() =>
      expect(r.json).toHaveBeenCalledWith('/api/contact/hcp/opt-in', expect.objectContaining({
        body: JSON.stringify({ session_id: 's1', use: false }),
      })),
    )
  })

  it('an opted-in contact can see and take back a grant', async () => {
    const opted = {
      ...base, opted_in: true, session,
      grants: [{ grant_id: 'urn:hcp:g1', agent: 'Echo', type: 'temporary', features: ['record'], expires_at: null }],
    }
    const r = rest(opted)
    render(<ContactMemory hcp={contactHcp(r)} sessionId="s1" agentName="Echo" />)
    fireEvent.click(await screen.findByText('What canopy knows'))
    expect(screen.getByText(/Echo: learn · this conversation/)).toBeTruthy()
    fireEvent.click(screen.getByText('Take back'))
    await waitFor(() =>
      expect(r.json).toHaveBeenCalledWith('/api/contact/hcp/grants/urn%3Ahcp%3Ag1', expect.objectContaining({
        method: 'DELETE',
      })),
    )
  })
})
