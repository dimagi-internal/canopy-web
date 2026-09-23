// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { AgentRunnerRuleOut } from '@/api/agents'
import type { RunnerOut } from '@/api/harness'

const getAgentRunnerRules = vi.fn<() => Promise<AgentRunnerRuleOut[]>>()
const putAgentRunnerRules = vi.fn<(slug: string, rules: unknown[]) => Promise<AgentRunnerRuleOut[]>>()
const listRunners = vi.fn<() => Promise<RunnerOut[]>>()

vi.mock('@/api/agents', () => ({ getAgentRunnerRules, putAgentRunnerRules }))
vi.mock('@/api/harness', () => ({ listRunners }))
// The last row's controls have their own tests; here they only need to render.
vi.mock('@/components/agents/RunnerAssignments', () => ({ RunnerAssignments: () => <div>default-runners</div> }))
vi.mock('@/components/agents/TurnModeToggle', () => ({ TurnModeToggle: () => <div>agent-mode</div> }))

const { AgentRouting } = await import('./AgentRouting')

const fleet = [
  { id: 'r-cloud', name: 'cloud-1', kind: 'cloud', status: 'online', ready: true },
  { id: 'r-mbp', name: 'jj-mbp', kind: 'emdash', status: 'online', ready: true },
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
  putAgentRunnerRules.mockImplementation(async () => rules)
  render(<AgentRouting agentSlug="eva" initialTurnMode="manual" />)
  await screen.findByTestId('routing-default-row')
}

describe('AgentRouting', () => {
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

  it('warns that a named auto rule holds only for verified mail', async () => {
    await mount([rule({ actor: 'beth@dimagi.com', turn_mode: 'auto' })])
    expect(screen.getByTestId('runner-rule-verified-email-beth@dimagi.com').textContent)
      .toMatch(/verified as coming from beth@dimagi.com/)
  })

  it('adds "email from beth -> cloud, auto" in one step', async () => {
    await mount([])
    fireEvent.click(screen.getByTestId('runner-rules-add-toggle'))
    const form = screen.getByTestId('routing-add-row')
    fireEvent.change(within(form).getByRole('textbox', { name: 'Sender' }), { target: { value: 'beth@dimagi.com' } })
    fireEvent.change(within(form).getByRole('combobox', { name: 'Mode' }), { target: { value: 'auto' } })
    fireEvent.click(screen.getByTestId('routing-add-submit'))

    await waitFor(() => expect(putAgentRunnerRules).toHaveBeenCalled())
    expect(putAgentRunnerRules.mock.calls[0][1]).toEqual([{
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
