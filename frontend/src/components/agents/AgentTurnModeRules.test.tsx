// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { TurnModeRuleOut } from '@/api/agents'

const getTurnModeRules = vi.fn<() => Promise<TurnModeRuleOut[]>>()
const setTurnModeRule = vi.fn<(slug: string, rule: unknown) => Promise<TurnModeRuleOut[]>>()
const deleteTurnModeRule = vi.fn<(slug: string, rule: unknown) => Promise<void>>()

vi.mock('@/api/agents', () => ({ getTurnModeRules, setTurnModeRule, deleteTurnModeRule }))
vi.mock('@/components/agents/TurnModeToggle', () => ({ TurnModeToggle: () => <div>agent-mode</div> }))

const { AgentTurnModeRules, sortTurnModeRules } = await import('./AgentTurnModeRules')

function rule(over: Partial<TurnModeRuleOut>): TurnModeRuleOut {
  return { source: '', actor: '', turn_mode: 'manual', set_by_email: '', updated_at: null, ...over }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

async function mount(rules: TurnModeRuleOut[]) {
  getTurnModeRules.mockResolvedValue(rules)
  setTurnModeRule.mockImplementation(async () => rules)
  render(<AgentTurnModeRules agentSlug="gshubdesk" initialTurnMode="manual" />)
  await screen.findByTestId('turn-mode-default-row')
}

describe('AgentTurnModeRules', () => {
  it('orders rules as the server reads them: person on a channel, person, channel', () => {
    const sorted = sortTurnModeRules([
      rule({ source: 'api' }),
      rule({ actor: 'beth@dimagi.com' }),
      rule({ source: 'email', actor: 'beth@dimagi.com' }),
    ])
    expect(sorted.map((r) => `${r.source}/${r.actor}`)).toEqual(['email/beth@dimagi.com', '/beth@dimagi.com', 'api/'])
  })

  it('adds "API turns from one login run auto" without naming a runner', async () => {
    await mount([])
    fireEvent.click(screen.getByTestId('turn-mode-rules-add-toggle'))
    const form = screen.getByTestId('turn-mode-add-row')
    fireEvent.change(within(form).getByRole('textbox', { name: 'Person' }), {
      target: { value: 'gshub-integration@dimagi.com' },
    })
    fireEvent.change(within(form).getByRole('combobox', { name: 'Channel' }), { target: { value: 'api' } })
    fireEvent.change(within(form).getByRole('combobox', { name: 'Mode' }), { target: { value: 'auto' } })
    fireEvent.click(screen.getByTestId('turn-mode-add-submit'))
    await waitFor(() => expect(setTurnModeRule).toHaveBeenCalled())
    expect(setTurnModeRule.mock.calls[0][1]).toEqual({
      source: 'api', actor: 'gshub-integration@dimagi.com', turn_mode: 'auto',
    })
  })

  it('will not add a rule that names neither a person nor a channel', async () => {
    await mount([])
    fireEvent.click(screen.getByTestId('turn-mode-rules-add-toggle'))
    expect((screen.getByTestId('turn-mode-add-submit') as HTMLButtonElement).disabled).toBe(true)
  })

  it('says a named auto holds only for a verified message, only when there is one', async () => {
    await mount([rule({ actor: 'beth@dimagi.com', turn_mode: 'auto' })])
    expect(screen.getByTestId('turn-mode-verified-note').textContent).toMatch(/verified as theirs/)
    cleanup()
    await mount([rule({ source: 'email', turn_mode: 'auto' })])
    expect(screen.queryByTestId('turn-mode-verified-note')).toBeNull()
  })

  it('shows the server refusal when a change is not allowed', async () => {
    await mount([rule({ source: 'api' })])
    setTurnModeRule.mockRejectedValueOnce(new Error('is for gshubdesk\'s owner or admins'))
    const row = screen.getByTestId('turn-mode-rule-api-anyone')
    fireEvent.change(within(row).getByRole('combobox'), { target: { value: 'auto' } })
    expect(await screen.findByText(/owner or admins/)).toBeTruthy()
  })
})
