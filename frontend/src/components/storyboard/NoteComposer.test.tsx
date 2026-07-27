// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { NoteComposer } from './NoteComposer'
import { AuthContext } from '@/auth/AuthProvider'

afterEach(cleanup)

// jsdom here exposes a `localStorage` object whose methods are missing, which
// is why the component guards every access — but a stub is needed to test that
// it remembers anything at all.
beforeEach(() => {
  const store = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
  })
})

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
    expect(screen.queryByText('Edit narrative')).toBeNull()

    cleanup()
    setup('suggest')
    fireEvent.click(screen.getByText('Leave a note'))
    expect(screen.getByText('Edit narrative')).toBeTruthy()
  })

  it('sends a comment in body and a suggestion in suggested_text', async () => {
    const { onSubmit, note } = setup('suggest')
    fireEvent.click(screen.getByText('Leave a note'))
    fireEvent.change(note(), { target: { value: 'Say back-check.' } })
    fireEvent.click(screen.getByText('Edit narrative'))
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

  it('asks for the reviewer’s name once, not once per note', async () => {
    // A full review puts a composer under every act, every demo and every
    // scene. Retyping a name twenty times is how you end up with twenty
    // "Anonymous" notes from the one person you sent the link to.
    const first = setup('comment')
    fireEvent.click(screen.getByText('Leave a note'))
    fireEvent.change(screen.getByPlaceholderText(/Your name/), { target: { value: 'Sophie' } })
    fireEvent.change(first.note(), { target: { value: 'one' } })
    fireEvent.click(screen.getByText('Leave note'))
    await waitFor(() => expect(first.onSubmit).toHaveBeenCalled())
    expect(first.onSubmit.mock.calls[0][0].author_name).toBe('Sophie')

    cleanup()
    const second = setup('comment')
    fireEvent.click(screen.getByText('Leave a note'))
    fireEvent.change(second.note(), { target: { value: 'two' } })
    fireEvent.click(screen.getByText('Leave note'))
    await waitFor(() => expect(second.onSubmit).toHaveBeenCalled())
    expect(second.onSubmit.mock.calls[0][0].author_name).toBe('Sophie')
  })

  it('hands the reviewer the real words to edit, rather than a blank box', async () => {
    // Describing an edit in prose ("in the second sentence say re-visit") is
    // work for the reader and a chance to be misread, when the reviewer already
    // knows the words they want.
    render(
      <NoteComposer
        capability="suggest"
        anchorLabel="Scene 2"
        cta="Leave a note"
        seedText="Tomas checks the register."
        defaults={{}}
        onSubmit={vi.fn().mockResolvedValue(undefined)}
      />,
    )
    fireEvent.click(screen.getByText('Leave a note'))
    fireEvent.click(screen.getByText('Edit narrative'))
    expect((screen.getByPlaceholderText(/Change the words/) as HTMLTextAreaElement).value).toBe(
      'Tomas checks the register.',
    )
  })

  it('never eats what the reviewer already typed', async () => {
    const { note } = setup('suggest')
    fireEvent.click(screen.getByText('Leave a note'))
    fireEvent.change(note(), { target: { value: 'half a thought' } })
    fireEvent.click(screen.getByText('Edit narrative'))
    expect((note() as HTMLTextAreaElement).value).toBe('half a thought')
  })

  it('does not ask a signed-in editor who they are', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(
      <AuthContext.Provider
        value={{
          status: 'authenticated',
          user: { name: 'Jonathan Jackson', email: 'jj@dimagi.com', avatar_url: '' },
        }}
      >
        <NoteComposer
          capability="comment"
          anchorLabel="Scene 1"
          cta="Leave a note"
          defaults={{}}
          onSubmit={onSubmit}
        />
      </AuthContext.Provider>,
    )
    fireEvent.click(screen.getByText('Leave a note'))
    await waitFor(() =>
      expect((screen.getByPlaceholderText(/Your name/) as HTMLInputElement).value).toBe(
        'Jonathan Jackson',
      ),
    )
    fireEvent.change(screen.getByPlaceholderText(/What’s wrong/), { target: { value: 'x' } })
    fireEvent.click(screen.getByText('Leave note'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onSubmit.mock.calls[0][0].author_name).toBe('Jonathan Jackson')
  })

  it('still asks an anonymous reviewer', () => {
    setup('comment')
    fireEvent.click(screen.getByText('Leave a note'))
    expect((screen.getByPlaceholderText(/Your name/) as HTMLInputElement).value).toBe('')
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
