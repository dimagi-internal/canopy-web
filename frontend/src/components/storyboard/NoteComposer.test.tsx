// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { NoteComposer } from './NoteComposer'

afterEach(cleanup)

function setup(capability: 'read' | 'comment' | 'suggest', onSubmit = vi.fn().mockResolvedValue(undefined)) {
  const { container } = render(
    <NoteComposer
      capability={capability}
      anchorLabel="Scene 1"
      cta="Leave a note"
      defaults={{ narrative_slug: 'verified-monitoring', target_version: 17 }}
      onSubmit={onSubmit}
    />,
  )
  // The composer has TWO text inputs (the note and the optional name), so
  // select the textarea explicitly rather than by role.
  const note = () => container.querySelector('textarea') as HTMLTextAreaElement
  return { onSubmit, note }
}

describe('NoteComposer', () => {
  it('renders nothing on a read-only link', () => {
    setup('read')
    expect(screen.queryByText('Leave a note')).toBeNull()
  })

  it('offers comment only, until the link grants suggest', () => {
    setup('comment')
    fireEvent.click(screen.getByText('Leave a note'))
    expect(screen.queryByText('Suggest wording')).toBeNull()

    cleanup()
    setup('suggest')
    fireEvent.click(screen.getByText('Leave a note'))
    expect(screen.getByText('Suggest wording')).toBeTruthy()
  })

  it('sends a comment in body and a suggestion in suggested_text', async () => {
    const { onSubmit, note } = setup('suggest')
    fireEvent.click(screen.getByText('Leave a note'))
    fireEvent.change(note(), { target: { value: 'Say back-check.' } })
    fireEvent.click(screen.getByText('Suggest wording'))
    fireEvent.click(screen.getByText('Leave note'))

    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    const payload = onSubmit.mock.calls[0][0]
    expect(payload.suggested_text).toBe('Say back-check.')
    expect(payload.body).toBe('')
    expect(payload.target_version).toBe(17)
  })

  it('lets a reviewer leave a SECOND note without reloading', async () => {
    // Someone reading a narrative leaves several notes on the same scene.
    const { onSubmit, note } = setup('comment')
    fireEvent.click(screen.getByText('Leave a note'))
    fireEvent.change(note(), { target: { value: 'first' } })
    fireEvent.click(screen.getByText('Leave note'))

    const again = await screen.findByText('Add another')
    fireEvent.click(again)
    fireEvent.change(note(), { target: { value: 'second' } })
    fireEvent.click(screen.getByText('Leave note'))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2))
    expect(onSubmit.mock.calls[1][0].body).toBe('second')
  })

  it('surfaces a failure instead of pretending it saved', async () => {
    const failing = vi.fn().mockRejectedValue(new Error('this link does not grant comment'))
    const { note } = setup('comment', failing)
    fireEvent.click(screen.getByText('Leave a note'))
    fireEvent.change(note(), { target: { value: 'x' } })
    fireEvent.click(screen.getByText('Leave note'))

    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'this link does not grant comment')
  })
})
