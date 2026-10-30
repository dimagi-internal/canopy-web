// @vitest-environment jsdom
//
// The inline "Unlock your vault key" form: it takes focus when it appears (it renders where the
// page put it, not where the person clicked), and answers with a passkey or a passphrase.
import { afterEach, describe, expect, it } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

import { useUnlockPrompt } from './UnlockPrompt'
import type { UnlockChoice } from './operations'

function Harness({ onResult }: { onResult: (r: UnlockChoice | Error) => void }) {
  const { prompt, element } = useUnlockPrompt()
  return (
    <div>
      <button type="button" onClick={() => prompt({ passkey: true, passphrase: true, needsSecretKey: false }).then(onResult, onResult)}>
        ask
      </button>
      {element}
    </div>
  )
}

afterEach(cleanup)

describe('useUnlockPrompt', () => {
  it('appears focused, and answers with the passkey', async () => {
    const results: Array<UnlockChoice | Error> = []
    render(<Harness onResult={(r) => results.push(r)} />)
    expect(screen.queryByTestId('vault-unlock')).toBeNull()
    fireEvent.click(screen.getByText('ask'))
    const passkey = await screen.findByTestId('vault-unlock-passkey')
    expect(document.activeElement).toBe(passkey)
    await act(async () => { fireEvent.click(passkey) })
    expect(results).toEqual([{ method: 'passkey' }])
    expect(screen.queryByTestId('vault-unlock')).toBeNull()
  })

  it('answers with a typed passphrase, and forgets it afterwards', async () => {
    const results: Array<UnlockChoice | Error> = []
    render(<Harness onResult={(r) => results.push(r)} />)
    fireEvent.click(screen.getByText('ask'))
    fireEvent.change(await screen.findByLabelText('Vault passphrase'), { target: { value: 'correct horse' } })
    await act(async () => { fireEvent.click(screen.getByTestId('vault-unlock-passphrase')) })
    expect(results).toEqual([{ method: 'passphrase', passphrase: 'correct horse', secretKey: undefined }])
    fireEvent.click(screen.getByText('ask'))
    expect(((await screen.findByLabelText('Vault passphrase')) as HTMLInputElement).value).toBe('')
  })

  it('rejects when cancelled, changing nothing', async () => {
    const results: Array<UnlockChoice | Error> = []
    render(<Harness onResult={(r) => results.push(r)} />)
    fireEvent.click(screen.getByText('ask'))
    await act(async () => { fireEvent.click(await screen.findByText('Cancel')) })
    expect((results[0] as Error).message).toContain('nothing was changed')
  })
})
