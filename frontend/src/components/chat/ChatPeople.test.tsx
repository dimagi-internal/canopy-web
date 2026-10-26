// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const listParticipants = vi.fn()
const addParticipant = vi.fn()
const removeParticipant = vi.fn()
const listParticipantCandidates = vi.fn()
vi.mock('@/api/chat', () => ({
  listParticipants: (...a: unknown[]) => listParticipants(...a),
  addParticipant: (...a: unknown[]) => addParticipant(...a),
  removeParticipant: (...a: unknown[]) => removeParticipant(...a),
  listParticipantCandidates: (...a: unknown[]) => listParticipantCandidates(...a),
  // An ordinary conversation: adding someone needs no key (ChatPeople.sealed.test.tsx).
  getSession: async () => ({ sensitive: false, sealed_key: null }),
  participantRecipient: vi.fn(),
  giveKeyCopy: vi.fn(),
}))

const { ChatPeoplePanel } = await import('./ChatPeople')

const OWNER = { user_id: 1, email: 'op@dimagi.com', display_name: 'Op', role: 'owner' }
const ED = { user_id: 2, email: 'ed@dimagi.com', display_name: 'Ed', role: 'editor' }
const NEW = { user_id: 3, email: 'new@dimagi.com', display_name: 'New', agent_slug: '' }
const ADA = { user_id: 4, email: 'ada@dimagi-ai.com', display_name: 'ada@dimagi-ai.com', agent_slug: 'ada' }

/** Type into the picker and choose the first suggestion. */
async function pickPerson(text: string, name: string) {
  const box = screen.getByRole('combobox', { name: 'People' })
  fireEvent.focus(box)
  fireEvent.change(box, { target: { value: text } })
  fireEvent.mouseDown(await screen.findByRole('option', { name: new RegExp(name) }))
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ChatPeoplePanel', () => {
  it("the chat's owner changes a role from the dropdown, removes, and adds", async () => {
    listParticipants.mockResolvedValue([OWNER, ED])
    listParticipantCandidates.mockResolvedValue([NEW, ADA])
    addParticipant.mockResolvedValue([OWNER, { ...ED, role: 'viewer' }])
    removeParticipant.mockResolvedValue([OWNER])
    render(<ChatPeoplePanel sessionId="s1" myRole="owner" />)

    await screen.findByText('Ed')
    // the owner's own row is fixed, with why
    expect(screen.queryByLabelText('Change role for op@dimagi.com')).toBeNull()
    expect(screen.getByText('owns the chat')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Change role for ed@dimagi.com'), { target: { value: 'viewer' } })
    await waitFor(() => expect(addParticipant).toHaveBeenCalledWith('s1', 'ed@dimagi.com', 'viewer'))
    await waitFor(() =>
      expect((screen.getByLabelText('Change role for ed@dimagi.com') as HTMLSelectElement).value).toBe('viewer'),
    )

    fireEvent.click(screen.getByLabelText('Remove ed@dimagi.com'))
    await waitFor(() => expect(removeParticipant).toHaveBeenCalledWith('s1', 2))
    await waitFor(() => expect(screen.queryByText('Ed')).toBeNull())

    addParticipant.mockResolvedValue([OWNER, { user_id: 3, email: 'new@dimagi.com', display_name: 'New', role: 'editor' }])
    await pickPerson('ne', 'New')
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(addParticipant).toHaveBeenCalledWith('s1', 'new@dimagi.com', 'editor'))
    expect((await screen.findAllByText('New')).length).toBeGreaterThan(0)
  })

  it('anyone else sees roles as text with no controls', async () => {
    listParticipants.mockResolvedValue([OWNER, ED])
    render(<ChatPeoplePanel sessionId="s1" myRole="editor" />)
    await screen.findByText('Ed')
    expect(screen.queryAllByRole('combobox')).toHaveLength(0)
    expect(screen.queryByLabelText('Remove ed@dimagi.com')).toBeNull()
    expect(screen.getByText(/only the chat's owner can add people/i)).toBeTruthy()
  })

  it("the agent's owner in the chat may add someone, but changes and removes nobody", async () => {
    listParticipants.mockResolvedValue([OWNER, ED])
    listParticipantCandidates.mockResolvedValue([NEW, ADA])
    addParticipant.mockImplementation(async (_id: string, email: string) =>
      [OWNER, ED, { user_id: email === NEW.email ? 3 : 4, email, display_name: email, role: 'editor' }])
    render(<ChatPeoplePanel sessionId="s1" myRole="editor" canAddPeople />)
    await screen.findByText('Ed')
    expect(screen.queryByLabelText('Change role for ed@dimagi.com')).toBeNull()
    expect(screen.queryByLabelText('Remove ed@dimagi.com')).toBeNull()
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'People' })).not.toHaveProperty('disabled', true))
    // Two picks, one Add: each is added in turn.
    await pickPerson('new', 'New')
    await pickPerson('ada', 'ada@dimagi-ai.com')
    fireEvent.click(screen.getByRole('button', { name: 'Add 2' }))
    await waitFor(() => expect(addParticipant).toHaveBeenCalledTimes(2))
    expect(addParticipant).toHaveBeenNthCalledWith(1, 's1', 'new@dimagi.com', 'editor')
    expect(addParticipant).toHaveBeenNthCalledWith(2, 's1', 'ada@dimagi-ai.com', 'editor')
  })

  it('offers only the people the server lists, and Enter never adds what was typed', async () => {
    listParticipants.mockResolvedValue([OWNER])
    listParticipantCandidates.mockResolvedValue([NEW])
    render(<ChatPeoplePanel sessionId="s1" myRole="owner" />)
    await screen.findByText('Op')
    const box = screen.getByRole('combobox', { name: 'People' })
    fireEvent.focus(box)
    fireEvent.change(box, { target: { value: 'stranger@example.com' } })
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(screen.getByText(/nobody in this workspace matches/i)).toBeTruthy()
    fireEvent.keyDown(box, { key: 'Enter' })
    expect((screen.getByRole('button', { name: 'Add' }) as HTMLButtonElement).disabled).toBe(true)
    expect(addParticipant).not.toHaveBeenCalled()
  })
})
