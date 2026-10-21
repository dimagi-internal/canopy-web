// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const getSessionInfo = vi.fn()
vi.mock('@/api/chat', () => ({
  getSessionInfo: (...a: unknown[]) => getSessionInfo(...a),
}))

const { SessionInfoPanel, credentialText } = await import('./SessionInfoPanel')

const TURN = {
  id: 't-1',
  origin: 'api',
  status: 'done',
  prompt: 'CC->ACE | 2026-10-21 | Task: MBW FLW drill check',
  result_note: '',
  content_hidden: false,
  created_at: '2026-10-21T10:25:46Z',
  claimed_at: '2026-10-21T10:25:47Z',
  finished_at: null,
  session_id: 'd8e1cf52-989a-46f5-91b9-ae78f6994507',
  claimed_by_name: 'haldimagi-mbp-cdp',
  enqueued_by_email: 'sid@dimagi.com',
  initiator: { kind: 'user', via: 'api', assurance: 'pat', self: false, client: '', user: { id: 9, email: 'sid@dimagi.com', name: 'Sid' } },
  turn_mode: 'manual',
  turn_mode_basis: 'agent',
  provenance: { credential: { type: 'pat', id: 47, label: 'DIM-SI-W144' }, client: 'canopy-cli/0.2.735' },
  parent_turn_id: null,
  parent_session_id: null,
  parent_task: '',
  parent_claude_session: '26a71d4e-9b49-4333-ba8c-ea8a1c697845',
}

const INFO = {
  session: {
    id: 's1',
    title: 'c-cc-ace-2026-10-21-task-mbw-flw-d4bf',
    agent_slug: 'ace',
    project: '',
    workspace: 'dimagi',
    status: 'active',
    origin: 'runner',
    created_at: '2026-10-21T10:25:51Z',
    last_activity_at: '2026-10-21T10:40:00Z',
    turn_mode: 'manual',
    turn_origin: 'api',
    runner_status: 'online',
    created_by: null,
    provenance: {},
    parent_turn_id: null,
    parent_session_id: null,
    parent_task: '',
    parent_claude_session: '26a71d4e-9b49-4333-ba8c-ea8a1c697845',
    activity: { repos: ['ace'] },
    slack: null,
  },
  metadata: {},
  contact: null,
  updated_at: '2026-10-21T10:40:00Z',
  binding: {
    runner_name: 'haldimagi-mbp-cdp',
    runner_kind: 'emdash',
    host: 'haldimagi@mbp',
    emdash_project: 'ace',
    session_key: 'c-cc-ace-2026-10-21-task-mbw-flw-d4bf',
    thread_key: 'dispatch-ace-b6b2806cd4bf',
    transcript_id: 'd8e1cf52-989a-46f5-91b9-ae78f6994507',
    task_uid: '',
    agent_task_ext_id: '',
    status: '',
    agent_status: '',
    summary: '',
  },
  turns: [TURN],
  turn_count: 1,
  turns_limit: 50,
  participant_count: 1,
  message_count: 12,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('SessionInfoPanel', () => {
  it('says who started the session, through what, and where it runs', async () => {
    getSessionInfo.mockResolvedValue(INFO)
    render(<SessionInfoPanel sessionId="s1" />)
    await screen.findByTestId('session-info')
    expect(getSessionInfo).toHaveBeenCalledWith('s1')
    expect(screen.getAllByText('Sid <sid@dimagi.com>').length).toBeGreaterThan(0)
    expect(screen.getByText('pat · 47 · DIM-SI-W144')).toBeTruthy()
    expect(screen.getByText('canopy-cli/0.2.735')).toBeTruthy()
    expect(screen.getByText('dispatch-ace-b6b2806cd4bf')).toBeTruthy()
    expect(screen.getAllByText('26a71d4e-9b49-4333-ba8c-ea8a1c697845').length).toBeGreaterThan(0)
  })

  it('Raw shows the whole stored record', async () => {
    getSessionInfo.mockResolvedValue(INFO)
    render(<SessionInfoPanel sessionId="s1" />)
    await screen.findByTestId('session-info')
    fireEvent.click(screen.getByTestId('session-info-raw-toggle'))
    expect(screen.getByTestId('session-info-raw').textContent).toContain('"thread_key": "dispatch-ace-b6b2806cd4bf"')
  })

  it('says why it could not load', async () => {
    getSessionInfo.mockRejectedValue(new Error('session not found'))
    render(<SessionInfoPanel sessionId="s1" />)
    expect(await screen.findByText('session not found')).toBeTruthy()
  })
})

describe('credentialText', () => {
  it('reads a browser session plainly and a token by type, id and label', () => {
    expect(credentialText({ type: 'session', id: null, label: '' })).toBe('browser session')
    expect(credentialText({ type: 'pat', id: 47, label: 'x' })).toBe('pat · 47 · x')
    expect(credentialText(undefined)).toBe('')
  })
})
