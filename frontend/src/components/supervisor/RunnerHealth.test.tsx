// @vitest-environment jsdom
//
// A box's own report of which features work. The bar this has to clear is the
// 2026-09-22 incident: cloud-ec2-1 read online + ready while transcripts, the
// inbox and ACP were all off — so a failed check must be impossible to miss, and
// "not reported" must never look like "healthy".
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { RunnerOut } from '@/api/harness'

const refreshRunner = vi.fn<(id: string) => Promise<RunnerOut>>()
vi.mock('@/api/harness', () => ({ refreshRunner }))

const { RunnerHealth } = await import('./RunnerHealth')
const { RunnerStatus } = await import('./RunnerStatus')

function runner(overrides: Partial<RunnerOut> = {}): RunnerOut {
  return {
    id: 'r1', name: 'cloud-ec2-1', kind: 'cloud', status: 'online', status_note: '',
    ready: true, ready_note: '', paused: false, paused_note: '', paused_at: null,
    last_heartbeat_at: '2026-09-22T12:00:00Z', capabilities: {}, host: '',
    code_branch: '', code_version: '', code_sha: '', expected_code_sha: '',
    code_committed_at: 0, expected_code_committed_at: 0, workspace: 'dimagi',
    paired_by_email: 'jj@dimagi.com', can_manage: true, can_administer: true,
    drill_rollup: null,
    ...overrides,
  }
}

const BROKEN = {
  health_checks: {
    'code_clone': { name: 'code_clone', status: 'ok' as const, detail: 'at origin/main abc123' },
    'packages.transcripts': {
      name: 'packages.transcripts', status: 'fail' as const,
      detail: 'canopy_transcript did not import: chat sessions on this box write no durable rows',
    },
    'claude.credentials': { name: 'claude.credentials', status: 'warn' as const, detail: 'one credential' },
  },
  health_received_at: '2026-09-22T12:00:00Z',
  health_bootstrapped_at: 1790000000,
}

afterEach(() => {
  cleanup()
  refreshRunner.mockReset()
})

describe('RunnerHealth', () => {
  it('lists failures first and says what is off', () => {
    render(<RunnerHealth runner={runner(BROKEN)} />)
    const rows = screen.getAllByTestId(/^runner-health-(code_clone|packages|claude)/)
    expect(rows.map((r) => r.dataset.testid)).toEqual([
      'runner-health-packages.transcripts', 'runner-health-claude.credentials', 'runner-health-code_clone',
    ])
    expect(screen.getByTestId('runner-health-packages.transcripts').textContent).toContain('no durable rows')
  })

  it('says "not reported" rather than showing a clean bill of health', () => {
    render(<RunnerHealth runner={runner()} />)
    expect(screen.getByTestId('runner-health-age').textContent).toBe('not reported')
    expect(screen.queryByTestId(/^runner-health-packages/)).toBeNull()
  })

  it('renders nothing for a runner that neither reports nor can be refreshed', () => {
    const { container } = render(<RunnerHealth runner={runner({ kind: 'emdash' })} />)
    expect(container.innerHTML).toBe('')
  })

  it('refresh asks the server and shows the request as pending', async () => {
    const onChanged = vi.fn()
    refreshRunner.mockResolvedValue(runner({ ...BROKEN, refresh_pending: true }))
    render(<RunnerHealth runner={runner(BROKEN)} onChanged={onChanged} />)
    fireEvent.click(screen.getByTestId('runner-refresh'))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(refreshRunner).toHaveBeenCalledWith('r1')
    cleanup()
    render(<RunnerHealth runner={runner({ ...BROKEN, refresh_pending: true })} />)
    expect((screen.getByTestId('runner-refresh') as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByTestId('runner-refresh-pending')).toBeTruthy()
  })

  it('hides refresh from someone who may not administer the box', () => {
    render(<RunnerHealth runner={runner({ ...BROKEN, can_administer: false })} />)
    expect(screen.queryByTestId('runner-refresh')).toBeNull()
  })

  it('says so when the request fails', async () => {
    refreshRunner.mockRejectedValue(new Error('refreshRunner failed: 404'))
    render(<RunnerHealth runner={runner(BROKEN)} />)
    fireEvent.click(screen.getByTestId('runner-refresh'))
    await waitFor(() => expect(screen.getByTestId('runner-refresh-error').textContent).toContain('404'))
  })
})

describe('RunnerStatus health badge', () => {
  it('counts problems and goes red when any check failed', () => {
    render(<RunnerStatus runners={[runner(BROKEN)]} />)
    const badge = screen.getByTestId('runner-health-badge-cloud-ec2-1')
    expect(badge.textContent).toBe('2 issues')
    expect(badge.className).toContain('text-destructive')
  })

  it('is amber when there are only warnings, and absent when all is well', () => {
    const warnOnly = {
      health_checks: { 'claude.credentials': { name: 'claude.credentials', status: 'warn' as const, detail: '' } },
    }
    render(<RunnerStatus runners={[runner(warnOnly), runner({ id: 'r2', name: 'clean', health_checks: {} })]} />)
    expect(screen.getByTestId('runner-health-badge-cloud-ec2-1').className).toContain('text-warning')
    expect(screen.queryByTestId('runner-health-badge-clean')).toBeNull()
  })
})
