// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const listParticipants = vi.fn()
const addParticipant = vi.fn()
const removeParticipant = vi.fn()
vi.mock('@/api/chat', () => ({
  listParticipants: (...a: unknown[]) => listParticipants(...a),
  addParticipant: (...a: unknown[]) => addParticipant(...a),
  removeParticipant: (...a: unknown[]) => removeParticipant(...a),
}))

const { ChatPeoplePanel } = await import('./ChatPeople')

const OWNER = { user_id: 1, email: 'op@dimagi.com', display_name: 'Op', role: 'owner' }
const ED = { user_id: 2, email: 'ed@dimagi.com', display_name: 'Ed', role: 'editor' }

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ChatPeoplePanel', () => {
  it("the chat's owner changes a role from the dropdown, removes, and adds", async () => {
    listParticipants.mockResolvedValue([OWNER, ED])
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
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'new@dimagi.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(addParticipant).toHaveBeenCalledWith('s1', 'new@dimagi.com', 'editor'))
    expect(await screen.findByText('New')).toBeTruthy()
  })

  it('anyone else sees roles as text with no controls', async () => {
    listParticipants.mockResolvedValue([OWNER, ED])
    render(<ChatPeoplePanel sessionId="s1" myRole="editor" />)
    await screen.findByText('Ed')
    expect(screen.queryAllByRole('combobox')).toHaveLength(0)
    expect(screen.queryByLabelText('Remove ed@dimagi.com')).toBeNull()
    expect(screen.getByText(/only the chat's owner can add people/i)).toBeTruthy()
  })
})
