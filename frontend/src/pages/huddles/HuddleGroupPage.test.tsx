// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'

const group = {
  slug: 'champions', workspace: 'connect', name: 'AI Champions', topic: 'Adopt AI', lead_email: 'alice@x.org',
  lead_slack_id: 'U1', channel_id: 'C1', channel_name: 'ai-champions', agents: ['hal'], leader_agent: 'hal',
  mode: 'single', cron: '0 9 * * 1', timezone: 'UTC', next_runs: [], schedule_id: 1, enabled: true,
  project: { agent: 'hal', ref: 'P3', name: 'AI Champions', status: 'active', url: '/w/connect/agents/hal/projects/P3' },
  last_run_status: 'done', url: '', can_edit: false, created_at: '', updated_at: '',
}
const run = {
  id: 7, group: 'champions', group_name: 'AI Champions', workspace: 'connect', slot: '2026-10-19T09:00:00Z',
  manual: false, status: 'done', note: '', announce_ts: '1700000999.000100',
  slack_url: 'https://slack.com/archives/C1/p1700000999000100', confirmed_by_email: 'alice@x.org',
  confirmed_at: null, read_messages: 12, read_first_ts: '1700000100.0', read_last_ts: '1700000500.0',
  read_since: null, material: '', turn_id: 't', huddle_id: 'h1', result_url: '', summary: '3 new tasks',
  finished_at: null, created_at: '',
}
vi.mock('@/api/huddleGroups', () => ({
  getHuddleGroup: vi.fn(async () => group), listHuddleGroupRuns: vi.fn(async () => [run]),
  deleteHuddleGroup: vi.fn(), createHuddleGroup: vi.fn(), updateHuddleGroup: vi.fn(),
}))
vi.mock('@/api/agents', () => ({ listAgents: vi.fn(async () => ({ items: [] })) }))
vi.mock('@/api/workspaces', () => ({ listMembers: vi.fn(async () => []) }))
const { HuddleGroupPage } = await import('./HuddleGroupPage')
const { readWindow } = await import('./workGroupModel')
afterEach(cleanup)

it('shows the group and its runs, with the read window, the Slack post and the huddle', async () => {
  render(<MemoryRouter initialEntries={['/w/connect/huddles/groups/champions']}>
    <Routes><Route path="/w/:workspace/huddles/groups/:slug" element={<HuddleGroupPage />} /></Routes></MemoryRouter>)
  expect(await screen.findByRole('heading', { name: /AI Champions/ })).toBeTruthy()
  expect(screen.getByText(/read 12 messages/)).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Slack post' }).getAttribute('href')).toBe(run.slack_url)
  expect(screen.getByRole('link', { name: 'The huddle' }).getAttribute('href')).toBe('/w/connect/huddles/h1')
  expect(screen.getByRole('link', { name: 'AI Champions' }).getAttribute('href')).toBe(group.project.url)
  expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
})

it('says plainly when nothing was read', () => {
  expect(readWindow({ ...run, status: 'awaiting_confirm' })).toBe('nothing read')
  expect(readWindow({ ...run, status: 'running', read_messages: 0 })).toBe('nothing new')
})
