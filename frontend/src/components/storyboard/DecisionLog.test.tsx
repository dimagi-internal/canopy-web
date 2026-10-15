// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, fireEvent } from '@testing-library/react'
import type { Decision } from '@/api/storyboards'
import { DecisionRow, groupDecisions } from './DecisionLog'

const base: Decision = {
  id: 'banner', scope: '', question: 'q', plain_question: 'Rename the banner before Edesia sees it?',
  options: ['Rename and re-record', 'Keep as is'], recommended: 'Rename and re-record', override: '',
  value: 'Rename and re-record', reasoning: 'The internal name is in every cut.', override_reasoning: '',
  source: '', evidence_basis: 'stated', conflict_signals: [], status: 'ai-default', revisit_when: '',
  review_ask: 'required-before', needed_by: 'external-share', confirm_reason: '', decided_by: '',
  decided_by_verified: false, decided_at: null, confirmed: false, history: [], supersedes: '',
  superseded_by: '', editable: true,
}
const row = (over: Partial<Decision>): Decision => ({ ...base, ...over })

afterEach(cleanup)

describe('groupDecisions', () => {
  it('puts asks first, then confirmations, then assumptions, then deferred; each row once', () => {
    const groups = groupDecisions([
      row({ id: 'a', review_ask: '', needed_by: '' }),
      row({ id: 'd', review_ask: '', needed_by: '', status: 'deferred', revisit_when: 'after Nov 12' }),
      row({ id: 'c', review_ask: 'recommended-confirmation', needed_by: '' }),
      row({ id: 'r' }),
      row({ id: 's', superseded_by: 'r' }),
    ])
    expect(groups.map((g) => g.title)).toEqual([
      'Answer before sharing', 'Please confirm', 'What the agent assumed', 'Not needed now', 'Replaced',
    ])
    expect(groups.map((g) => g.rows.map((r) => r.id))).toEqual([['r'], ['c'], ['a'], ['d'], ['s']])
    expect(groups.map((g) => g.open)).toEqual([true, true, false, false, false])
  })

  it('puts a required row not needed before sharing under Please confirm', () => {
    const groups = groupDecisions([
      row({ id: 'n', review_ask: 'required-before', needed_by: 'next-run' }),
      row({ id: 'm', review_ask: 'required-before', needed_by: '' }),
    ])
    expect(groups.map((g) => g.title)).toEqual(['Please confirm'])
    expect(groups[0].rows.map((r) => r.id)).toEqual(['n', 'm'])
  })

  it('omits empty groups', () => {
    expect(groupDecisions([row({ id: 'a', review_ask: '', needed_by: '' })]).map((g) => g.title))
      .toEqual(['What the agent assumed'])
  })
})

describe('DecisionRow', () => {
  it('shows the plain question, the value and a recommended chip', () => {
    render(<DecisionRow d={base} canEdit={false} onEdit={vi.fn()} />)
    expect(screen.getByText('Rename the banner before Edesia sees it?')).toBeTruthy()
    expect(screen.getByText('Rename and re-record')).toBeTruthy()
    expect(screen.getByText('Recommended')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /confirm/i })).toBeNull()
  })

  it('shows who changed it', () => {
    render(<DecisionRow d={row({ status: 'overridden', override: 'Keep as is', value: 'Keep as is',
      decided_by: 'Lily' })} canEdit={false} onEdit={vi.fn()} />)
    expect(screen.getByText('Changed by Lily')).toBeTruthy()
  })

  it('falls back to plain chips when names are missing', () => {
    render(<DecisionRow d={row({ confirmed: true, decided_by: '' })} canEdit={false} onEdit={vi.fn()} />)
    expect(screen.getByText('Confirmed')).toBeTruthy()
  })

  it('confirms through onEdit', async () => {
    const onEdit = vi.fn().mockResolvedValue(undefined)
    render(<DecisionRow d={base} canEdit onEdit={onEdit} />)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(onEdit).toHaveBeenCalledWith({ action: 'confirm' })
  })

  it('a read-only caveat offers no actions even to an editor', () => {
    render(<DecisionRow d={row({ editable: false })} canEdit onEdit={vi.fn()} />)
    expect(screen.queryByRole('button', { name: 'Confirm' })).toBeNull()
  })
})
