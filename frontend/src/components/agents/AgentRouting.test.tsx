// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { AgentRunnerRuleOut } from '@/api/agents'
import type { RunnerOut } from '@/api/harness'

const getAgentRunnerRules = vi.fn<() => Promise<AgentRunnerRuleOut[]>>()
const saveAgentRunnerRules = vi.fn<(slug: string, prev: unknown[], next: unknown[]) => Promise<AgentRunnerRuleOut[]>>()
const listRunners = vi.fn<() => Promise<RunnerOut[]>>()

vi.mock('@/api/agents', () => ({ getAgentRunnerRules, saveAgentRunnerRules }))
vi.mock('@/api/harness', () => ({ listRunners }))
// The last row's controls have their own tests; here they only need to render.
vi.mock('@/components/agents/RunnerAssignments', () => ({ RunnerAssignments: () => <div>default-runners</div> }))
vi.mock('@/components/agents/AgentDefaultRunners', () => ({ AgentDefaultRunners: () => <div>default-runners</div> }))
vi.mock('@/components/agents/TurnModeToggle', () => ({ TurnModeToggle: () => <div>agent-mode</div> }))

const { AgentRouting } = await import('./AgentRouting')

const fleet = [
  { id: 'r-cloud', name: 'cloud-1', kind: 'cloud', status: 'online', ready: true, flags: ['zdr'] },
  { id: 'r-mbp', name: 'jj-mbp', kind: 'emdash', status: 'online', ready: true, flags: [] },
] as unknown as RunnerOut[]

function rule(over: Partial<AgentRunnerRuleOut>): AgentRunnerRuleOut {
  return {
    source: 'email', actor: '', rank: 0, runner_id: 'r-cloud', runner_name: 'cloud-1',
    kind: 'cloud', strict: false, online: true, ready: true, enabled: true, queued_count: 0,
    turn_mode: '', ...over,
  }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

async function mount(rules: AgentRunnerRuleOut[]) {
  getAgentRunnerRules.mockResolvedValue(rules)
  listRunners.mockResolvedValue(fleet)
  saveAgentRunnerRules.mockImplementation(async () => rules)
  render(<AgentRouting agentSlug="eva" initialTurnMode="manual" />)
  await screen.findByTestId('routing-default-row')
}

describe('AgentRouting', () => {
  it('badges a runner that declares ZDR, and only that one', async () => {
    await mount([
      rule({ actor: '' }),
      rule({ actor: 'beth@dimagi.com', runner_id: 'r-mbp', runner_name: 'jj-mbp' }),
    ])
    expect(screen.getByTestId('zdr-badge-cloud-1')).toBeTruthy()
    expect(screen.queryByTestId('zdr-badge-jj-mbp')).toBeNull()
  })

  it('lists the rules above the agent\'s own defaults, in evaluation order', async () => {
    await mount([rule({ actor: '' }), rule({ actor: 'beth@dimagi.com' })])
    const rows = screen.getAllByRole('row').map((r) => r.getAttribute('data-testid'))
    // header, then the named rule, then its source's anyone-rule, then the defaults
    expect(rows.slice(1)).toEqual([
      'runner-rule-email-beth@dimagi.com', 'runner-rule-email', 'routing-default-row',
    ])
  })

  it('says what a rule with no mode inherits', async () => {
    await mount([rule({ actor: '', turn_mode: 'auto' }), rule({ actor: 'beth@dimagi.com' })])
    const beth = screen.getByTestId('runner-rule-email-beth@dimagi.com')
    const select = within(beth).getByRole('combobox', { name: /Mode for/ }) as HTMLSelectElement
    expect(select.selectedOptions[0].textContent).toBe('As below (Auto)')
  })

  it('says once, under the table, that a named auto rule holds only for verified mail', async () => {
    await mount([
      rule({ actor: 'beth@dimagi.com', turn_mode: 'auto' }),
      rule({ source: 'api', actor: 'jj@dimagi.com', turn_mode: 'auto', runner_id: 'r-mbp', runner_name: 'jj-mbp' }),
    ])
    expect(screen.getAllByTestId('routing-verified-note')).toHaveLength(1)
    expect(screen.getByTestId('routing-verified-note').textContent).toMatch(/verified as theirs/)
  })

  it('leaves the verified note out when no named rule runs auto', async () => {
    await mount([rule({ actor: 'beth@dimagi.com' })])
    expect(screen.queryByTestId('routing-verified-note')).toBeNull()
    // the detail sits behind one disclosure
    expect(screen.getByText('How routing works')).toBeTruthy()
  })

  it('draws identical rules for several kinds of work as one row', async () => {
    const jj = { actor: 'jj@dimagi.com', runner_id: 'r-mbp', runner_name: 'jj-mbp' }
    await mount([
      rule({ source: 'api', ...jj }),
      rule({ source: 'canopy_web_chat', ...jj }),
      rule({ source: 'email', ...jj }),
    ])
    const row = screen.getByTestId('runner-rule-api+canopy_web_chat+email-jj@dimagi.com')
    expect(within(row).getByText('other (API)')).toBeTruthy()
    expect(within(row).getByText('canopy chat')).toBeTruthy()
    expect(within(row).getByText('email')).toBeTruthy()

    // an edit to the row is an edit to every rule under it
    fireEvent.click(within(row).getByRole('radio', { name: 'Wait' }))
    await waitFor(() => expect(saveAgentRunnerRules).toHaveBeenCalled())
    const next = saveAgentRunnerRules.mock.calls[0][2] as { source: string; strict: boolean }[]
    expect(next.map((r) => [r.source, r.strict])).toEqual([
      ['api', true], ['canopy_web_chat', true], ['email', true],
    ])
  })

  it('removes one kind of work from a merged row, and adds another', async () => {
    const jj = { actor: 'jj@dimagi.com', runner_id: 'r-mbp', runner_name: 'jj-mbp' }
    await mount([rule({ source: 'api', ...jj }), rule({ source: 'email', ...jj })])
    const row = screen.getByTestId('runner-rule-api+email-jj@dimagi.com')
    fireEvent.click(within(row).getByRole('button', { name: 'Stop routing email by this rule' }))
    await waitFor(() => expect(saveAgentRunnerRules).toHaveBeenCalledTimes(1))
    expect((saveAgentRunnerRules.mock.calls[0][2] as { source: string }[]).map((r) => r.source)).toEqual(['api'])
  })

  it('adds a kind of work to a rule, copying its runners and sender', async () => {
    await mount([rule({ actor: 'jj@dimagi.com', runner_id: 'r-mbp', runner_name: 'jj-mbp', strict: true })])
    const row = screen.getByTestId('runner-rule-email-jj@dimagi.com')
    const add = within(row).getByRole('combobox', { name: /Add a kind of work/ }) as HTMLSelectElement
    // a schedule has no sender, so it cannot join a named rule
    expect(Array.from(add.options).map((o) => o.value)).not.toContain('canopy_scheduler')
    fireEvent.change(add, { target: { value: 'canopy_web_chat' } })
    await waitFor(() => expect(saveAgentRunnerRules).toHaveBeenCalled())
    expect(saveAgentRunnerRules.mock.calls[0][2]).toEqual([
      { source: 'email', actor: 'jj@dimagi.com', runnerIds: ['r-mbp'], strict: true, turnMode: '' },
      { source: 'canopy_web_chat', actor: 'jj@dimagi.com', runnerIds: ['r-mbp'], strict: true, turnMode: '' },
    ])
  })

  it('adds "email from beth -> cloud, auto" in one step', async () => {
    await mount([])
    fireEvent.click(screen.getByTestId('runner-rules-add-toggle'))
    const form = screen.getByTestId('routing-add-row')
    fireEvent.change(within(form).getByRole('textbox', { name: 'Sender' }), { target: { value: 'beth@dimagi.com' } })
    fireEvent.change(within(form).getByRole('combobox', { name: 'Mode' }), { target: { value: 'auto' } })
    fireEvent.click(screen.getByTestId('routing-add-submit'))

    await waitFor(() => expect(saveAgentRunnerRules).toHaveBeenCalled())
    expect(saveAgentRunnerRules.mock.calls[0][2]).toEqual([{
      source: 'email', actor: 'beth@dimagi.com', runnerIds: ['r-cloud'], strict: false, turnMode: 'auto',
    }])
  })

  it('refuses to add a rule that already exists', async () => {
    await mount([rule({ actor: '' })])
    fireEvent.click(screen.getByTestId('runner-rules-add-toggle'))
    expect((screen.getByTestId('routing-add-submit') as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/email from anyone already has a row/)).toBeTruthy()
  })

  it('offers no sender field for scheduled work, which has no sender', async () => {
    await mount([rule({ source: 'canopy_scheduler' })])
    const row = screen.getByTestId('runner-rule-canopy_scheduler')
    expect(within(row).queryByRole('textbox')).toBeNull()
  })
})
