// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ArrivalDomains } from './ConnectedAppsPage'

afterEach(cleanup)

describe('ArrivalDomains', () => {
  it('says plainly that everyone is a contact when nothing is set, and offers no edit to a non-owner', () => {
    render(<ArrivalDomains value={[]} editable={false} signs onSave={() => {}} />)
    expect(screen.getByTestId('arrival-domains').textContent).toMatch(/every visitor is a contact/)
    expect(screen.queryByRole('button', { name: 'Change' })).toBeNull()
  })

  it('warns when domains are set but the site signs nothing, so nobody can resolve', () => {
    render(<ArrivalDomains value={['dimagi.com']} editable signs={false} onSave={() => {}} />)
    expect(screen.getByText(/signs no assertions yet/)).toBeTruthy()
  })

  it('an owner edits the list, comma- or space-separated', () => {
    const onSave = vi.fn()
    render(<ArrivalDomains value={['dimagi.com']} editable signs onSave={onSave} />)
    fireEvent.click(screen.getByRole('button', { name: 'Change' }))
    fireEvent.change(screen.getByLabelText('Arrival domains'), { target: { value: 'dimagi.com,  dimagi-ai.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(onSave).toHaveBeenCalledWith(['dimagi.com', 'dimagi-ai.com'])
  })
})
