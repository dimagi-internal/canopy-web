// @vitest-environment jsdom
//
// System superusers: listed, added by email, removed — never the last one (the button
// is not offered when only one is left; the server refuses it too). No email setting:
// the role brings none.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { OperatorOut } from '@/api/harness'

const listOperators = vi.fn<() => Promise<OperatorOut[]>>()
const addOperator = vi.fn<(email: string) => Promise<OperatorOut>>()
const removeOperator = vi.fn<(id: number) => Promise<void>>()
vi.mock('@/api/harness', () => ({ listOperators, addOperator, removeOperator }))

const { OperatorsCard } = await import('./OperatorsCard')

const jj: OperatorOut = { user_id: 1, email: 'jj@dimagi.com', name: 'Jonathan', is_you: true }
const lucina: OperatorOut = { user_id: 2, email: 'lucina@dimagi.com', name: '', is_you: false }

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('OperatorsCard', () => {
  it('renders nothing for someone who is not a system superuser', () => {
    const { container } = render(<OperatorsCard show={false} />)
    expect(container.innerHTML).toBe('')
    expect(listOperators).not.toHaveBeenCalled()
  })

  it('adds and removes a system superuser by email', async () => {
    listOperators.mockResolvedValueOnce([jj]).mockResolvedValueOnce([jj, lucina]).mockResolvedValue([jj])
    addOperator.mockResolvedValue(lucina)
    removeOperator.mockResolvedValue()
    render(<OperatorsCard show />)

    await waitFor(() => expect(screen.getAllByTestId('operator-row')).toHaveLength(1))
    expect(screen.queryByTestId('operator-remove')).toBeNull() // never the last one
    expect(screen.queryByTestId('operator-alert-mail')).toBeNull()

    fireEvent.change(screen.getByTestId('operator-email'), { target: { value: 'lucina@dimagi.com' } })
    fireEvent.click(screen.getByTestId('operator-add'))
    await waitFor(() => expect(screen.getAllByTestId('operator-row')).toHaveLength(2))
    expect(addOperator).toHaveBeenCalledWith('lucina@dimagi.com')

    fireEvent.click(screen.getAllByTestId('operator-remove')[1])
    await waitFor(() => expect(removeOperator).toHaveBeenCalledWith(2))
  })

  it('says why an add failed', async () => {
    listOperators.mockResolvedValue([jj])
    addOperator.mockRejectedValue(new Error('no one has signed in to Canopy as x@dimagi.com yet'))
    render(<OperatorsCard show />)
    await waitFor(() => screen.getByTestId('operator-email'))

    fireEvent.change(screen.getByTestId('operator-email'), { target: { value: 'x@dimagi.com' } })
    fireEvent.click(screen.getByTestId('operator-add'))

    await waitFor(() => expect(screen.getByTestId('operators-error').textContent).toContain('signed in'))
  })
})
