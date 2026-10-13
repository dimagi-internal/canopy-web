// @vitest-environment jsdom
//
// Admission rules as a workspace setting: everyone sees which turns are
// refused, only a manager gets the controls, the rule an owner's notice links
// to is highlighted, and each rule says how often it has fired.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import type { AdmissionRuleOut, AdmissionRulesOut } from '@/api/workspaces'

const { getAdmissionRules, createAdmissionRule, updateAdmissionRule, deleteAdmissionRule, listAgents,
  setAdmissionSenderNotices } =
  vi.hoisted(() => ({
    setAdmissionSenderNotices: vi.fn(),
    getAdmissionRules: vi.fn<(slug: string) => Promise<AdmissionRulesOut>>(),
    createAdmissionRule: vi.fn(),
    updateAdmissionRule: vi.fn(),
    deleteAdmissionRule: vi.fn(),
    listAgents: vi.fn(),
  }))

vi.mock('@/api/workspaces', async (orig) => ({
  ...(await orig<typeof import('@/api/workspaces')>()),
  getAdmissionRules,
  createAdmissionRule,
  updateAdmissionRule,
  deleteAdmissionRule,
  setAdmissionSenderNotices,
}))
vi.mock('@/api/agents', () => ({ listAgents }))

const { WorkspaceAdmissionRulesPage } = await import('./WorkspaceAdmissionRulesPage')

const pairs = (...values: string[]) => values.map((v) => ({ value: v, label: v }))
const choices = {
  origins: pairs('email', 'slack'),
  principals: pairs('member', 'contact'),
  senders: pairs('blocked'),
  access: pairs('confined', 'full', 'none'),
  turn_modes: pairs('manual', 'auto'),
  runner_kinds: pairs('emdash', 'cloud'),
  reaches: pairs('paused', 'offline'),
  actions: pairs('reject', 'allow', 'auto'),
}

const cloud: AdmissionRuleOut = {
  id: 3, workspace: 'connect', agent: '', origin: 'email', principal: '', sender: '', access: 'confined',
  capability: '', turn_mode: 'manual', runner_kind: 'cloud', reach: '', wait_hours: 24, action: 'reject',
  reason: 'nobody can approve the reply', notify_sender: true, notify_owner: true, enabled: true,
  summary: 'Email, confined, manual, on a Cloud runner', seeded: true, refusal: false, fired_count: 2,
  last_fired_at: '2026-10-10T23:46:00Z', created_by: '', updated_at: '2026-10-11T00:00:00Z',
}
const laptop: AdmissionRuleOut = {
  ...cloud, id: 4, runner_kind: 'emdash', enabled: false, fired_count: 0, last_fired_at: null,
  summary: 'Email, confined, manual, on a Laptop (emdash) runner',
}

function policy(over: Partial<AdmissionRulesOut> = {}): AdmissionRulesOut {
  return {
    workspace: 'connect', can_manage: true, sender_notices_enabled: false,
    rules: [cloud, laptop], choices, ...over,
  }
}

function show(path = '/w/connect/settings/admission-rules') {
  listAgents.mockResolvedValue({ items: [{ slug: 'hal', name: 'Hal', workspace: 'connect' }], total: 1 })
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/w/:workspace/settings/admission-rules" element={<WorkspaceAdmissionRulesPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('WorkspaceAdmissionRulesPage', () => {
  it('lists the rules with how often each fired, and says sender replies are off', async () => {
    getAdmissionRules.mockResolvedValue(policy())
    show()
    expect((await screen.findByTestId('admission-rule-3')).textContent).toContain('on a Cloud runner')
    expect(screen.getByTestId('admission-fired-3').textContent).toContain('2×')
    expect(screen.getByTestId('admission-fired-4').textContent).toContain('never')
    expect((screen.getByTestId('admission-toggle-4') as HTMLInputElement).checked).toBe(false)
    expect(screen.getByTestId('admission-sender-notices').textContent).toContain('Off in this workspace')
  })

  it('highlights the rule an owner notice links to', async () => {
    getAdmissionRules.mockResolvedValue(policy())
    show('/w/connect/settings/admission-rules?rule=3')
    expect((await screen.findByTestId('admission-rule-3')).getAttribute('data-highlighted')).toBe('true')
    expect(screen.getByTestId('admission-rule-4').getAttribute('data-highlighted')).toBeNull()
  })

  it('toggles a rule on and off with a partial update', async () => {
    getAdmissionRules.mockResolvedValue(policy())
    updateAdmissionRule.mockResolvedValue({ ...laptop, enabled: true })
    show()
    fireEvent.click(await screen.findByTestId('admission-toggle-4'))
    await waitFor(() => expect(updateAdmissionRule).toHaveBeenCalledWith('connect', 4, { enabled: true }))
    expect(getAdmissionRules).toHaveBeenCalledTimes(2)
  })

  it('adds a rule, and edits one in place', async () => {
    getAdmissionRules.mockResolvedValue(policy())
    createAdmissionRule.mockResolvedValue(cloud)
    updateAdmissionRule.mockResolvedValue(cloud)
    show()
    await screen.findByTestId('admission-form')
    fireEvent.change(screen.getByLabelText('Action'), { target: { value: 'allow' } })
    await screen.findByRole('option', { name: 'Hal' })
    fireEvent.change(screen.getByLabelText('Agent'), { target: { value: 'hal' } })
    fireEvent.change(screen.getByLabelText('Channel'), { target: { value: 'email' } })
    fireEvent.change(screen.getByLabelText('Why'), { target: { value: 'hal answers its own mail' } })
    fireEvent.click(screen.getByTestId('admission-save'))
    await waitFor(() => expect(createAdmissionRule).toHaveBeenCalledWith('connect', expect.objectContaining({
      action: 'allow', agent: 'hal', origin: 'email', reason: 'hal answers its own mail',
    })))

    fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0])
    expect((screen.getByLabelText('Runner') as HTMLSelectElement).value).toBe('cloud')
    fireEvent.change(screen.getByLabelText('Why'), { target: { value: 'changed' } })
    fireEvent.click(screen.getByTestId('admission-save'))
    await waitFor(() => expect(updateAdmissionRule).toHaveBeenCalledWith(
      'connect', 3, expect.objectContaining({ reason: 'changed', runner_kind: 'cloud' })))
  })

  it('marks a refusal rule as one that cannot let a turn run, and edits its sender', async () => {
    const blocked: AdmissionRuleOut = {
      ...cloud, id: 5, access: '', turn_mode: '', runner_kind: '', sender: 'blocked', refusal: true,
      notify_sender: false, notify_owner: false, summary: 'Email from a blocked sender',
    }
    getAdmissionRules.mockResolvedValue(policy({ rules: [cloud, blocked] }))
    updateAdmissionRule.mockResolvedValue(blocked)
    show()
    expect((await screen.findByTestId('admission-refusal-5')).textContent).toContain('always refused')
    expect(screen.queryByTestId('admission-refusal-3')).toBeNull()
    fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[1])
    expect((screen.getByLabelText('Sender') as HTMLSelectElement).value).toBe('blocked')
    fireEvent.click(screen.getByTestId('admission-save'))
    await waitFor(() => expect(updateAdmissionRule).toHaveBeenCalledWith(
      'connect', 5, expect.objectContaining({ sender: 'blocked' })))
  })

  it('marks a run-in-auto-mode rule and gives it no notices', async () => {
    const auto: AdmissionRuleOut = { ...cloud, id: 9, action: 'auto', turn_mode: '', access: '', origin: '',
      summary: 'Any turn to ace, on a Cloud runner' }
    getAdmissionRules.mockResolvedValue(policy({ rules: [auto] }))
    show()
    const row = await screen.findByTestId('admission-rule-9')
    expect(row.textContent).toContain('auto')
    expect(row.textContent).toContain('—')
  })

  it('lets an admin switch automatic replies on for the workspace', async () => {
    getAdmissionRules.mockResolvedValue(policy())
    setAdmissionSenderNotices.mockResolvedValue(true)
    show()
    fireEvent.click(await screen.findByTestId('admission-sender-notices-toggle'))
    await waitFor(() => expect(setAdmissionSenderNotices).toHaveBeenCalledWith('connect', true))
    expect(getAdmissionRules).toHaveBeenCalledTimes(2)
  })

  it('shows a reader the rules and no controls', async () => {
    getAdmissionRules.mockResolvedValue(policy({ can_manage: false }))
    show()
    await screen.findByTestId('admission-rules')
    expect(screen.queryByTestId('admission-form')).toBeNull()
    expect(screen.queryByTestId('admission-delete-3')).toBeNull()
    expect((screen.getByTestId('admission-toggle-3') as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByTestId('admission-sender-notices-toggle') as HTMLInputElement).disabled).toBe(true)
  })
})
