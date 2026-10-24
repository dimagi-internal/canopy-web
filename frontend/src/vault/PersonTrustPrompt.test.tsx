// @vitest-environment jsdom
// A person's vault key that CHANGED since this browser trusted it is a stop: there is no
// button that continues until the adder types the new fingerprint the person read them.
import { act, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { fingerprintMatches, usePersonTrust, type ConfirmPerson } from './PersonTrustPrompt'
import { fingerprint } from './zk'

const THUMB = 'abcdEFGHijklMNOPqrstUVWXyz012345abcdEFGHijk'

function Harness({ onReady }: { onReady: (c: ConfirmPerson) => void }) {
  const t = usePersonTrust()
  onReady(t.confirm)
  return <div>{t.element}</div>
}

describe('a changed key', () => {
  it('continues only on the typed fingerprint', async () => {
    let confirm: ConfirmPerson | null = null
    render(<Harness onReady={(c) => { confirm = c }} />)
    let answer: Promise<boolean> | null = null
    act(() => {
      answer = confirm!({ email: 'peer@dimagi.com', verdict: { status: 'changed', jkt: THUMB, was: 'older' } })
    })
    expect(screen.queryByTestId('person-trust-ok')).toBeNull()
    // The new fingerprint is not shown to copy: it must come from the person.
    expect(screen.queryByTestId('person-trust-fingerprint')).toBeNull()
    fireEvent.change(screen.getByTestId('person-trust-typed'), { target: { value: 'AAAA BBBB' } })
    expect(screen.queryByTestId('person-trust-ok')).toBeNull()
    expect(screen.getByTestId('person-trust-mismatch')).toBeTruthy()
    fireEvent.change(screen.getByTestId('person-trust-typed'),
                     { target: { value: fingerprint(THUMB).toLowerCase().replace(/ /g, '') } })
    fireEvent.click(screen.getByTestId('person-trust-ok'))
    await expect(answer).resolves.toBe(true)
  })

  it('a new key is still a fingerprint shown and a click', async () => {
    let confirm: ConfirmPerson | null = null
    render(<Harness onReady={(c) => { confirm = c }} />)
    let answer: Promise<boolean> | null = null
    act(() => {
      answer = confirm!({ email: 'peer@dimagi.com', verdict: { status: 'new', jkt: THUMB } })
    })
    expect(screen.getByTestId('person-trust-fingerprint').textContent).toBe(fingerprint(THUMB))
    fireEvent.click(screen.getByTestId('person-trust-ok'))
    await expect(answer).resolves.toBe(true)
  })

  it('matches spaces and case aside, and nothing else', () => {
    expect(fingerprintMatches(fingerprint(THUMB).toLowerCase(), THUMB)).toBe(true)
    expect(fingerprintMatches('', THUMB)).toBe(false)
    expect(fingerprintMatches(fingerprint(THUMB).slice(0, 10), THUMB)).toBe(false)
  })
})
