// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { AuthContext } from '@/auth/AuthProvider'
import { memberWaiting, VaultWaitingNote } from './VaultWaiting'

afterEach(cleanup)

const ed = { email: 'ed@dimagi.com', has_vault_key: true, has_copy: false }
const jj = { email: 'jj@dimagi.com', has_vault_key: true, has_copy: true }

function renderAs(email: string, holders: object) {
  return render(
    <AuthContext.Provider value={{ status: 'authenticated', user: { email, name: '', avatar_url: '' } as never }}>
      <VaultWaitingNote members={[jj, ed]} holders={holders} testId="note" />
    </AuthContext.Provider>,
  )
}

describe('VaultWaiting', () => {
  it('says who waits and that a keeper passes it on', () => {
    renderAs('jj@dimagi.com', { has_key: true, people: ['jj@dimagi.com'], keepers: ['labs-cloud-1'] })
    const text = screen.getByTestId('note').textContent ?? ''
    expect(text).toContain('ed@dimagi.com is waiting')
    expect(text).toContain('labs-cloud-1 passes it on by itself')
  })

  it('tells the waiting person themselves, and who must grant when no runner will', () => {
    renderAs('ed@dimagi.com', { has_key: true, people: ['jj@dimagi.com'], keepers: [] })
    const text = screen.getByTestId('note').textContent ?? ''
    expect(text).toContain('You are waiting')
    expect(text).toContain('jj@dimagi.com must grant it')
  })

  it('warns when nobody can pass it on', () => {
    renderAs('jj@dimagi.com', { has_key: true, people: [], keepers: [] })
    expect(screen.getByTestId('note').textContent).toContain('Nobody who holds it can pass it on')
  })

  it('says nothing before the vault has a key, and labels each member', () => {
    renderAs('jj@dimagi.com', { has_key: false })
    expect(screen.queryByTestId('note')).toBeNull()
    expect(memberWaiting({ email: 'x', has_vault_key: false }, { has_key: true })).toBe('hasn’t set up their vault key')
    expect(memberWaiting(ed, { has_key: true })).toBe('waiting for the key')
    expect(memberWaiting(jj, { has_key: true })).toBeNull()
  })
})
