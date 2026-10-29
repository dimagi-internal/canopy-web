// @vitest-environment jsdom
//
// Superusers: listed with their alert mail, added by email, removed — never the last
// one (the button is not offered when only one is left; the server refuses it too).
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { OperatorOut } from '@/api/harness'

const listOperators = vi.fn<() => Promise<OperatorOut[]>>()
const addOperator = vi.fn<(email: string) => Promise<OperatorOut>>()
const removeOperator = vi.fn<(id: number) => Promise<void>>()
const setOperatorAlertMail = vi.fn<(id: number, v: OperatorOut['alert_mail']) => Promise<OperatorOut>>()
vi.mock('@/api/harness', () => ({ listOperators, addOperator, removeOperator, setOperatorAlertMail }))

const { OperatorsCard } = await import('./OperatorsCard')

const jj: OperatorOut = { user_id: 1, email: 'jj@dimagi.com', name: 'Jonathan', alert_mail: 'all', is_you: true }
const lucina: OperatorOut = { user_id: 2, email: 'lucina@dimagi.com', name: '', alert_mail: 'assigned', is_you: false }

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('OperatorsCard', () => {
  it('renders nothing for someone who is not a superuser', () => {
    const { container } = render(<OperatorsCard show={false} />)
    expect(container.innerHTML).toBe('')
    expect(listOperators).not.toHaveBeenCalled()
  })

  it('adds a superuser by email and sets their alert mail', async () => {
    listOperators.mockResolvedValueOnce([jj]).mockResolvedValue([jj, { ...lucina, alert_mail: 'all' }])
    addOperator.mockResolvedValue(lucina)
    setOperatorAlertMail.mockResolvedValue(lucina)
    render(<OperatorsCard show />)

    await waitFor(() => expect(screen.getAllByTestId('operator-row')).toHaveLength(1))
    expect(screen.queryByTestId('operator-remove')).toBeNull() // never the last one

    fireEvent.change(screen.getByTestId('operator-email'), { target: { value: 'lucina@dimagi.com' } })
    fireEvent.click(screen.getByTestId('operator-add'))
    await waitFor(() => expect(screen.getAllByTestId('operator-row')).toHaveLength(2))
    expect(addOperator).toHaveBeenCalledWith('lucina@dimagi.com')

    fireEvent.change(screen.getAllByTestId('operator-alert-mail')[1], { target: { value: 'assigned' } })
    await waitFor(() => expect(setOperatorAlertMail).toHaveBeenCalledWith(2, 'assigned'))
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
