// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, fireEvent } from '@testing-library/react'
import type { Decision } from '@/api/storyboards'
import { DecisionLog, DecisionRow } from './DecisionLog'
import * as api from '@/api/storyboards'

vi.mock('@/auth/AuthProvider', () => ({ useAuth: () => ({ status: 'anonymous' }) }))
vi.mock('@/api/storyboards', async () => {
  const actual = await vi.importActual<typeof api>('@/api/storyboards')
  return { ...actual, editDecision: vi.fn() }
})
import { groupDecisions, narrativeTitles } from './decisionGroups'
import type { Storyboard } from '@/api/storyboards'

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

describe('scope', () => {
  const titles = new Map([['supply-rutf', 'RUTF supply']])
  const assumed = (over: Partial<Decision>) => row({ review_ask: '', needed_by: '', ...over })

  it('splits what the agent assumed by scope: whole set first, then each demo by its title', () => {
    const groups = groupDecisions([
      assumed({ id: 'n1', scope: 'supply-rutf' }),
      assumed({ id: 'w1', scope: '' }),
      assumed({ id: 'u1', scope: 'unknown-slug' }),
      assumed({ id: 'n2', scope: 'supply-rutf' }),
    ], titles)
    const sub = groups[0].byScope!
    expect(sub.map((s) => s.label)).toEqual(['Whole set', 'RUTF supply', 'One demo'])
    expect(sub.map((s) => s.rows.map((r) => r.id))).toEqual([['w1'], ['n1', 'n2'], ['u1']])
  })

  it('only the assumptions group is split by scope', () => {
    const groups = groupDecisions([row({ id: 'r', scope: 'supply-rutf' })], titles)
    expect(groups[0].byScope).toBeUndefined()
  })

  it('reads narrative titles off the board entries', () => {
    const board = { acts: [{ anchor_id: 'act:1', title: 'I', prose: '', entries: [
      { narrative_slug: 'supply-rutf', title: 'RUTF supply' }] }] } as unknown as Storyboard
    expect(narrativeTitles(board).get('supply-rutf')).toBe('RUTF supply')
  })

  it('renders scope subheadings and a title tag on asks, never the slug', () => {
    const board = {
      slug: 'b', acts: [{ anchor_id: 'act:1', title: 'I', prose: '', entries: [
        { narrative_slug: 'supply-rutf', title: 'RUTF supply' }] }],
      decisions: [
        row({ id: 'ask', scope: 'supply-rutf' }),
        assumed({ id: 'w', scope: '', plain_question: 'Whole-set default' }),
        assumed({ id: 'n', scope: 'supply-rutf', plain_question: 'Demo default' }),
      ],
    } as unknown as Storyboard
    const { container } = render(<DecisionLog board={board} canEdit={false} />)
    expect(screen.getByRole('heading', { name: 'Whole set' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'RUTF supply' })).toBeTruthy()
    // The ask row carries the narrative's title as a tag; nothing shows the slug.
    expect(screen.getAllByText('RUTF supply')).toHaveLength(2)
    expect(container.textContent).not.toContain('supply-rutf')
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

  it('shows pending edits from the round', () => {
    render(<DecisionRow d={{ ...base, pending: [{ edit_id: '1', key: 'banner', action: 'pick',
      value: 'Keep as is', reasoning: '', by_name: 'Sophie', at: '' }] }} canEdit={false} onEdit={vi.fn()} />)
    expect(screen.getByText('Pending: Sophie → Keep as is')).toBeTruthy()
  })

  it('asks a nameless guest for a name instead of showing buttons', () => {
    render(<DecisionRow d={base} canEdit needsName onEdit={vi.fn()} />)
    expect(screen.getByText('Add your name in a note first')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Confirm' })).toBeNull()
  })
})

describe('guest staging in a round', () => {
  const guestBoard = { slug: 'b', acts: [], decisions: [base], capability: 'suggest', round: { number: 1 } } as unknown as Storyboard

  let store: Record<string, string>
  beforeEach(() => {
    store = {}
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v },
    })
  })
  afterEach(() => vi.unstubAllGlobals())

  it('sends the remembered name with the edit', async () => {
    store['canopy.reviewer-name'] = 'Sophie'
    ;(api.editDecision as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(base)
    render(<DecisionLog board={guestBoard} canEdit={false} token="tok" ws="connect" />)
    fireEvent.click(screen.getByRole('button', { name: 'Keep as is' }))
    await vi.waitFor(() => expect(api.editDecision).toHaveBeenCalledWith(
      'b', 'banner', { action: 'pick', value: 'Keep as is', author_name: 'Sophie' }, 'tok', 'connect'))
  })

  it('shows the add-your-name hint, not buttons, when no name is remembered', () => {
    render(<DecisionLog board={guestBoard} canEdit={false} />)
    expect(screen.getByText('Add your name in a note first')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Keep as is' })).toBeNull()
  })
})
