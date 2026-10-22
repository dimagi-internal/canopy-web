// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { conflictsOf, RoundBanner } from './RoundBanner'
import type { StagedEdit, Storyboard } from '@/api/storyboards'

afterEach(cleanup)

const edit = (o: Partial<StagedEdit>): StagedEdit =>
  ({ edit_id: 'e', key: 'banner', action: 'pick', value: 'Keep as is', reasoning: '', by_name: 'S', at: '', ...o })

describe('conflictsOf', () => {
  it('flags rows whose staged edits disagree, not rows where people agree', () => {
    const c = conflictsOf([
      edit({ edit_id: '1' }), edit({ edit_id: '2', by_name: 'L', value: 'keep as IS' }),
      edit({ edit_id: '3', key: 'sqlns', action: 'confirm', value: '' }),
      edit({ edit_id: '4', key: 'sqlns', value: 'Add SQ-LNS' }),
    ])
    expect(Object.keys(c)).toEqual(['sqlns'])
  })

  it('counts a write-in as a pick', () => {
    const c = conflictsOf([
      edit({ edit_id: '1' }), edit({ edit_id: '2', action: 'write_in', value: ' Keep  as is ' }),
    ])
    expect(Object.keys(c)).toEqual([])
  })
})

const board = (round: Storyboard['round'], extra: Partial<Storyboard> = {}): Storyboard => ({
  slug: 'supply', title: 'Connect Supply', lede: '', capability: 'suggest', layout: 'review',
  is_member: true, can_manage: false, acts: [], decisions: [], round, previous_round: null,
  ...extra,
})

describe('RoundBanner', () => {
  it('offers an editor the Open button when no round is open', () => {
    render(<RoundBanner board={board(null)} canManage onChanged={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Open a feedback round' })).toBeTruthy()
  })

  it('shows counts and asks the closer to pick where people disagreed', () => {
    const round = { number: 1, state: 'open', opened_at: '', opened_by: 'Jonathan', closed_at: null,
      close_note: '', pinned_versions: {}, note_count: 7, people_count: 3, notes: [],
      staged_edits: [edit({ edit_id: '1' }), edit({ edit_id: '2', action: 'confirm', value: '' })] }
    render(<RoundBanner board={board(round, { decisions: [{ id: 'banner', plain_question: 'Rename?', recommended: 'Rename' } as never] })} canManage onChanged={vi.fn()} />)
    expect(screen.getByText(/7 notes from 3 people/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Close round and start the next run' }))
    expect(screen.getByText(/People disagreed/)).toBeTruthy()
    expect(screen.getAllByRole('radio')).toHaveLength(2)
    // Server refuses an unpicked conflict, so the button waits for a pick.
    const go = screen.getByRole('button', { name: 'Close and hand to the agent' }) as HTMLButtonElement
    expect(go.disabled).toBe(true)
    fireEvent.click(screen.getAllByRole('radio')[0])
    expect(go.disabled).toBe(false)
  })

  it('a non-editor sees the round but no controls', () => {
    const round = { number: 2, state: 'open', opened_at: '', opened_by: '', closed_at: null,
      close_note: '', pinned_versions: {}, note_count: 0, people_count: 0, notes: [], staged_edits: [] }
    render(<RoundBanner board={board(round)} canManage={false} onChanged={vi.fn()} />)
    expect(screen.getByText(/Feedback round 2 is open/)).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
  })

  const open = (decisions: Storyboard['decisions']) => {
    const round = { number: 1, state: 'open', opened_at: '', opened_by: '', closed_at: null, close_note: '',
      pinned_versions: {}, note_count: 0, people_count: 0, notes: [],
      staged_edits: [edit({ edit_id: '1' }), edit({ edit_id: '2', action: 'confirm', value: '' })] }
    render(<RoundBanner board={board(round, { decisions })} canManage onChanged={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Close round and start the next run' }))
  }

  it('needs no pick for a removed or replaced row', () => {
    open([{ id: 'other' } as never])
    expect(screen.queryAllByRole('radio')).toHaveLength(0)
    expect((screen.getByRole('button', { name: 'Close and hand to the agent' }) as HTMLButtonElement).disabled).toBe(false)
    cleanup()
    open([{ id: 'banner', superseded_by: 'x' } as never])
    expect(screen.queryAllByRole('radio')).toHaveLength(0)
    expect((screen.getByRole('button', { name: 'Close and hand to the agent' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('says the round is with the agent after close, with no Open button', () => {
    const prev = { number: 3, state: 'closed', opened_at: '', opened_by: '', closed_at: '', close_note: '',
      pinned_versions: {}, note_count: 0, people_count: 0, notes: [], staged_edits: [] }
    render(<RoundBanner board={{ ...board(null), previous_round: prev }} canManage onChanged={vi.fn()} />)
    expect(screen.getByText(/Round 3 is closed and with the agent/)).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
  })
})
