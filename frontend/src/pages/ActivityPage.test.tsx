// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

vi.mock('@/api/turns', () => ({
  listTurns: vi.fn(async () => [{
    id: 't1', agent_slug: 'hal', project: '', target: 'hal', workspace_slug: 'connect',
    origin: 'api', status: 'done', routing: 'any', prompt: '', origin_ref: {},
    claimed_by_name: 'cloud', enqueued_by_email: null, initiator: {}, session_id: '',
    result_note: '', content_hidden: true, created_at: new Date().toISOString(),
  }]),
  listTurnEvents: vi.fn(async () => []),
}))
vi.mock('@/api/logs', () => ({
  listEvents: vi.fn(async () => [{
    id: 1, workspace: 'connect', source: 'runner', kind: 'offline', level: 'error', key: 'k',
    summary: 'cloud-ec2-1 went offline', payload: {}, count: 3,
    first_seen_at: new Date().toISOString(), last_seen_at: new Date().toISOString(),
  }]),
  listMcpCalls: vi.fn(async () => []),
}))

const { default: ActivityPage } = await import('./ActivityPage')

afterEach(cleanup)

function at(url: string) {
  render(<MemoryRouter initialEntries={[url]}><ActivityPage /></MemoryRouter>)
}

describe('ActivityPage logs', () => {
  it('opens the event log from ?log=events', async () => {
    at('/w/connect/activity?log=events')
    expect(await screen.findByText('cloud-ec2-1 went offline')).toBeTruthy()
  })

  it('says who can see MCP calls when there are none', async () => {
    at('/w/connect/activity?log=mcp')
    expect(await screen.findByTestId('mcp-calls-empty')).toBeTruthy()
  })

  it('shows the turn log by default', async () => {
    at('/w/connect/activity')
    expect(await screen.findByText('cloud')).toBeTruthy()
  })
})
