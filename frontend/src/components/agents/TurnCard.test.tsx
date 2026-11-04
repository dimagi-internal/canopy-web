// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { AgentTurnOut } from '@/api/agents'

const listTurnMessages = vi.fn(async () => ({
  messages: [{ turn_index: 0, role: 'assistant', content: {}, plaintext: 'Nothing to do today.' }],
  truncated: false,
}))

vi.mock('@/api/turns', () => ({
  listTurnEvents: vi.fn(async () => [{ seq: 1, ts: '2026-10-01T18:06:15Z', kind: 'claimed' }]),
  listTurnMessages,
}))

const { TurnCard } = await import('./cards')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const base: AgentTurnOut = {
  id: 't1', agent_slug: 'ace', cli_session_id: '', title: '', summary: '',
  task_ext_ids: [], work_product_urls: [], session_slug: '', share_token: '',
  started_at: '2026-10-01T18:06:15Z', ended_at: '2026-10-01T18:06:21Z', source: '',
  created_at: '2026-10-01T18:06:12Z', status: 'done', origin: 'api',
  session_key: 'c-fix-brief-61bb', emdash_task_id: 'c-fix-brief-61bb', reported_at: null,
  prompt: 'Fix the brief from Ada\nwith the details below', result_note: 'Opened PR #12', job: '', job_repo: '',
  origin_ref: {}, chat_session_id: null, has_transcript: false, content_hidden: false, closeout_verdict: '', closeout_line: '',
}

function renderCard(turn: AgentTurnOut) {
  return render(
    <MemoryRouter initialEntries={['/w/connect/agents/ace/turns']}>
      <Routes>
        <Route path="/w/:workspace/agents/:slug/turns" element={<TurnCard turn={turn} />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('TurnCard', () => {
  it('shows an unreported turn by its prompt, status and result', () => {
    renderCard(base)
    expect(screen.getByText('Fix the brief from Ada')).toBeTruthy()
    expect(screen.getByText('done')).toBeTruthy()
    expect(screen.getByText('Opened PR #12')).toBeTruthy()
    expect(screen.getByText(/no report/)).toBeTruthy()
  })

  it('opens to the full prompt and the event ledger', async () => {
    renderCard(base)
    const toggle = screen.getByRole('button', { expanded: false })
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByText(/with the details below/)).toBeTruthy()
    expect(await screen.findByText('claimed')).toBeTruthy()
  })

  it('links a turn to the chat that holds its work', () => {
    renderCard({ ...base, chat_session_id: 'e7bd5a55-9be3-4653-ba38-35a110a3e714' })
    const link = screen.getByRole('link', { name: /open chat/i })
    expect(link.getAttribute('href')).toBe('/w/connect/sessions/e7bd5a55-9be3-4653-ba38-35a110a3e714')
  })

  it('shows a cloud turn its own transcript, since it has no chat', async () => {
    renderCard({ ...base, session_key: '', emdash_task_id: '', has_transcript: true })
    expect(screen.queryByRole('link', { name: /open chat/i })).toBeNull()
    fireEvent.click(screen.getByRole('button', { expanded: false }))
    expect(await screen.findByText('Nothing to do today.')).toBeTruthy()
    expect(listTurnMessages).toHaveBeenCalledWith('t1')
  })

  it('does not fetch a transcript for a turn that has a chat', () => {
    renderCard({ ...base, chat_session_id: 'abc', has_transcript: true })
    fireEvent.click(screen.getByRole('button', { expanded: false }))
    expect(listTurnMessages).not.toHaveBeenCalled()
  })
})

describe('TurnCard for a plain job', () => {
  const job: AgentTurnOut = {
    ...base, prompt: '', job: 'sync-leads', job_repo: 'hal-jobs', result_note: 'exit code 0',
    has_transcript: false,
  } as AgentTurnOut

  it('names the job, not an empty prompt, and does not say the report is missing', () => {
    renderCard(job)
    expect(screen.getByText('Job: sync-leads from hal-jobs')).toBeTruthy()
    expect(screen.queryByText(/no report/)).toBeNull()
    expect(screen.queryByText(/transcript/i)).toBeNull()
  })

  it('opens to the output as a log, with no transcript, prompt or chat box', async () => {
    const { listTurnEvents } = await import('@/api/turns')
    vi.mocked(listTurnEvents).mockResolvedValueOnce([
      { seq: 1, ts: '2026-11-04T09:00:00Z', kind: 'status', payload: { source: 'job', text: 'wrote 12 rows' } },
    ] as never)
    const { container } = renderCard(job)
    fireEvent.click(screen.getByRole('button', { expanded: false }))
    expect(await screen.findByTestId('job-log')).toBeTruthy()
    expect(listTurnMessages).not.toHaveBeenCalled()
    expect(screen.queryByText('Prompt')).toBeNull()
    expect(container.querySelector('textarea, input')).toBeNull()
    expect(container.textContent?.toLowerCase()).not.toContain('transcript')
  })
})
