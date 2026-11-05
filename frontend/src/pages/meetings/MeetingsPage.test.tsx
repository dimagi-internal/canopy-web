// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const list = vi.fn()
const create = vi.fn()
vi.mock('@/api/meetings', async (original) => ({
  ...(await original<typeof import('@/api/meetings')>()),
  listMeetings: (p: unknown) => list(p),
  createMeeting: (b: unknown) => create(b),
}))
vi.mock('@/api/work', () => ({
  listProjects: () => Promise.resolve([{ workspace: 'connect', handle: 'weekly', name: 'Weekly' }]),
}))
vi.mock('@/api/agents', () => ({
  listMyAgents: () => Promise.resolve({ items: [{ slug: 'hal', name: 'Hal' }] }),
}))

import { linkOnly } from '@/api/meetings'
import MeetingsPage from './MeetingsPage'

const ROW = {
  id: 'm1', workspace: 'connect', title: 'Weekly sync', started_at: null, created_at: '2026-11-04T10:00:00Z',
  owner: 'Ana', is_mine: true, attendees: [], project: null, has: ['transcript:pasted'], snippet: 'we should ship',
}

function page() {
  return render(
    <MemoryRouter initialEntries={['/w/connect/meetings']}>
      <Routes>
        <Route path="/w/:workspace/meetings" element={<MeetingsPage />} />
        <Route path="/w/:workspace/meetings/:id" element={<p>opened</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  list.mockResolvedValue({ meetings: [ROW], has_more: false })
  create.mockResolvedValue({ id: 'm2' })
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('MeetingsPage', () => {
  it('says a meeting with no project is yours alone, and shows why a search matched', async () => {
    page()
    expect(await screen.findByText('Weekly sync')).toBeTruthy()
    expect(screen.getByText(/Just you/)).toBeTruthy()
    expect(screen.getByText('we should ship')).toBeTruthy()
  })

  it('searches with what was typed', async () => {
    page()
    await screen.findByText('Weekly sync')
    fireEvent.change(screen.getByLabelText('Search meetings'), { target: { value: 'budget' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(list).toHaveBeenLastCalledWith({ workspace: 'connect', q: 'budget' }))
  })

  it('treats pasted text as the notes and a lone link as a pointer', async () => {
    expect(linkOnly(' https://docs.google.com/document/d/1/edit ')).toBe('https://docs.google.com/document/d/1/edit')
    expect(linkOnly('see https://x.org')).toBeNull()
    page()
    await screen.findByText('Weekly sync')
    fireEvent.click(screen.getByRole('button', { name: 'Add a meeting' }))
    fireEvent.change(screen.getByPlaceholderText(/Paste the text/), {
      target: { value: 'https://docs.google.com/document/d/1/edit' },
    })
    expect(screen.getByText(/Your agent will read this link/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(create).toHaveBeenCalled())
    expect(create.mock.calls[0][0]).toMatchObject({
      workspace: 'connect',
      project: '',
      follow_up: false,
      artifact: { source: 'google_doc', source_url: 'https://docs.google.com/document/d/1/edit' },
    })
    expect(await screen.findByText('opened')).toBeTruthy()
  })

  it('asks for help only when an agent is chosen, and offers the projects as who can see it', async () => {
    page()
    await screen.findByText('Weekly sync')
    fireEvent.click(screen.getByRole('button', { name: 'Add a meeting' }))
    await screen.findByText('Hal')
    expect(screen.getByText('Everyone on Weekly')).toBeTruthy()
    fireEvent.change(screen.getByPlaceholderText(/Paste the text/), { target: { value: 'we talked' } })
    fireEvent.change(screen.getByLabelText('Agent to help'), { target: { value: 'hal' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save and get help' }))
    await waitFor(() => expect(create).toHaveBeenCalled())
    expect(create.mock.calls[0][0]).toMatchObject({ agent: 'hal', follow_up: true, artifact: { text: 'we talked', source: 'pasted' } })
  })
})
