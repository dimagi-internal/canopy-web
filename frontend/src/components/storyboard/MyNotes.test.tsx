// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { mineOf, MyNotes } from './MyNotes'
import type { RoundNote } from '@/api/storyboards'

afterEach(cleanup)

const note = (o: Partial<RoundNote>): RoundNote => ({ id: 1, kind: 'comment', body: 'b', suggested_text: '',
  author_name: 'Sophie', anchor_id: '', target_kind: 'narrative', target_ref: 'n', target_version: 3,
  state: 'answered', disposition_note: 'Slowed cut 1', resolved_in_version: 4, created_at: '', mine: false, ...o })

describe('MyNotes', () => {
  it('mine = flagged by the server, or (guest) the remembered name', () => {
    const notes = [note({ id: 1, mine: true, author_name: 'Lily' }), note({ id: 2 }), note({ id: 3, author_name: 'Bob' })]
    expect(mineOf(notes, 'sophie').map((n) => n.id)).toEqual([1, 2])
  })

  it('says what happened to each note', () => {
    const round = { number: 1, state: 'consumed', opened_at: '', opened_by: '', closed_at: null, close_note: '',
      pinned_versions: {}, note_count: 1, people_count: 1, notes: [note({ mine: true })], staged_edits: [] }
    render(<MyNotes round={round} viewerName="" />)
    expect(screen.getByText('Changed in version 4')).toBeTruthy()
    expect(screen.getByText('Slowed cut 1')).toBeTruthy()
  })
})
