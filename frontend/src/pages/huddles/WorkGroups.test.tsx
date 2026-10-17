// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'

const group = {
  slug: 'champions', workspace: 'connect', name: 'AI Champions', topic: 'Adopt AI', lead_email: 'alice@x.org',
  lead_slack_id: 'U1', channel_id: 'C1', channel_name: 'ai-champions', agents: ['hal'], leader_agent: 'hal',
  mode: 'single', cron: '0 9 * * 1', timezone: 'UTC', next_runs: [], schedule_id: 1, project: null, enabled: true,
  last_run_status: 'awaiting_confirm', url: '/w/connect/huddles/groups/champions', can_edit: true,
  created_at: '', updated_at: '',
}
const listHuddleGroups = vi.fn(async () => [group])
const createHuddleGroup = vi.fn(async (b: unknown) => b)
vi.mock('@/api/huddleGroups', () => ({ listHuddleGroups, createHuddleGroup, updateHuddleGroup: vi.fn() }))
vi.mock('@/api/agents', () => ({ listAgents: vi.fn(async () => ({ items: [{ slug: 'hal' }, { slug: 'eva' }] })) }))
vi.mock('@/api/workspaces', () => ({ listMembers: vi.fn(async () => [{ email: 'alice@x.org' }]) }))
let role = 'admin'
vi.mock('@/workspace/WorkspaceProvider', () => ({ useWorkspaceRole: () => role }))
const { WorkGroups } = await import('./WorkGroups')
afterEach(cleanup)

const show = () => render(<MemoryRouter><WorkGroups workspace="connect" /></MemoryRouter>)

it('lists work groups, linked to their page', async () => {
  show()
  const link = await screen.findByRole('link', { name: 'AI Champions' })
  expect(link.getAttribute('href')).toBe('/w/connect/huddles/groups/champions')
  expect(screen.getByText(/#ai-champions · lead alice@x.org/)).toBeTruthy()
  expect(screen.getByText(/last run awaiting confirm/)).toBeTruthy()
})

it('offers the form to admins only', async () => {
  role = 'editor'
  show()
  await screen.findByRole('link', { name: 'AI Champions' })
  expect(screen.queryByRole('button', { name: 'New work group' })).toBeNull()
  role = 'admin'
})

it('asks for a leader once there is more than one agent, and creates', async () => {
  show()
  fireEvent.click(await screen.findByRole('button', { name: 'New work group' }))
  const form = screen.getByRole('form', { name: 'New work group' })
  fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'Champs' } })
  fireEvent.change(screen.getByLabelText(/^Slug/), { target: { value: 'champs' } })
  fireEvent.change(screen.getByLabelText(/^Slack channel/), { target: { value: '#ai' } })
  fireEvent.change(screen.getByLabelText(/^Lead/), { target: { value: 'alice@x.org' } })
  fireEvent.click(await screen.findByLabelText('hal'))
  expect(screen.queryByLabelText(/^Leader/)).toBeNull()
  fireEvent.click(screen.getByLabelText('eva'))
  fireEvent.change(screen.getByLabelText(/^Leader/), { target: { value: 'eva' } })
  fireEvent.submit(form)
  await waitFor(() => expect(createHuddleGroup).toHaveBeenCalled())
  expect(createHuddleGroup.mock.calls[0][0]).toMatchObject({
    slug: 'champs', name: 'Champs', channel: '#ai', lead_email: 'alice@x.org', agents: ['hal', 'eva'],
    leader_agent: 'eva', cron: '0 9 * * 1',
  })
})
