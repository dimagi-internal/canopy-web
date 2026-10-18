// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'

const row = {
  id: 'work-fleet-20261006', type: 'work', team: 'fleet', leader: 'ada', members: ['eva', 'echo'],
  anchor_turn_id: 'a', created_at: new Date().toISOString(), finished: false, outcome_count: 2, rounds_dispatched: 2,
}
const listHuddles = vi.fn(async () => [row])
vi.mock('@/api/huddles', () => ({ listHuddles, getHuddle: vi.fn() }))
const checkin = {
  id: 4, name: 'Weekly check-in', workspace: 'connect', agent: 'ada', format: 'huddle', agents: ['ada', 'hal'],
  cron: '0 9 * * 2', timezone: 'America/Denver', channel_id: 'C1', channel_name: 'canopy-support',
  lead_email: 'jj@dimagi.com', enabled: true, url: '/w/connect/agents/ada/projects/P7',
  project: { agent: 'ada', ref: 'P7', name: 'Canopy support', url: '/w/connect/agents/ada/projects/P7' },
}
const listCheckins = vi.fn(async () => [checkin])
vi.mock('@/api/checkins', () => ({ listCheckins }))
const { HuddlesPage } = await import('./HuddlesPage')
afterEach(cleanup)

it('links each huddle to its page', async () => {
  render(<MemoryRouter initialEntries={['/w/connect/huddles']}>
    <Routes><Route path="/w/:workspace/huddles" element={<HuddlesPage />} /></Routes></MemoryRouter>)
  const link = await screen.findByRole('link', { name: /work-fleet-20261006/ })
  expect(link.getAttribute('href')).toBe('/w/connect/huddles/work-fleet-20261006')
  expect(screen.getByText(/in flight/i)).toBeTruthy()
})

it('says what a huddle is when there are none', async () => {
  listHuddles.mockResolvedValueOnce([])
  render(<MemoryRouter initialEntries={['/w/connect/huddles']}>
    <Routes><Route path="/w/:workspace/huddles" element={<HuddlesPage />} /></Routes></MemoryRouter>)
  expect(await screen.findByText(/no huddles yet/i)).toBeTruthy()
})

it('lists check-ins as links to their projects, where each one lives', async () => {
  render(<MemoryRouter initialEntries={['/w/connect/huddles']}>
    <Routes><Route path="/w/:workspace/huddles" element={<HuddlesPage />} /></Routes></MemoryRouter>)
  const link = await screen.findByRole('link', { name: /Canopy support/ })
  expect(link.getAttribute('href')).toBe('/w/connect/agents/ada/projects/P7#project-checkins')
  expect(screen.getByText(/#canopy-support/)).toBeTruthy()
})
