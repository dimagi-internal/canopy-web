// @vitest-environment jsdom
//
// The one-time recovery code: shown once, focus lands on it, Continue waits for "I have saved it".
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { RecoveryCodeBox } from './RecoveryCodeBox'

afterEach(cleanup)

function box(over: Partial<Parameters<typeof RecoveryCodeBox>[0]> = {}) {
  const onContinue = vi.fn()
  const onCancel = vi.fn()
  render(
    <RecoveryCodeBox testId="rc" savedTestId="rc-saved" continueTestId="rc-continue" title="Recovery code — shown once"
      code="AAAA-BBBB" filename="code.txt" fileText="AAAA-BBBB" onContinue={onContinue} onCancel={onCancel} {...over}>
      Keep it offline.
    </RecoveryCodeBox>,
  )
  return { onContinue, onCancel }
}

describe('RecoveryCodeBox', () => {
  it('shows the code and takes focus, without being a modal', () => {
    box()
    expect(screen.getByText('AAAA-BBBB')).toBeTruthy()
    const group = screen.getByTestId('rc')
    expect(group.getAttribute('role')).toBe('group')
    expect(group.getAttribute('aria-modal')).toBeNull()
    expect(document.activeElement).toBe(group)
  })

  it('lets Continue through only once the person says they saved it', () => {
    const { onContinue } = box()
    const go = screen.getByTestId('rc-continue') as HTMLButtonElement
    expect(go.disabled).toBe(true)
    fireEvent.click(screen.getByTestId('rc-saved'))
    expect(go.disabled).toBe(false)
    fireEvent.click(go)
    expect(onContinue).toHaveBeenCalled()
  })

  it('offers Cancel only when the caller can cancel', () => {
    cleanup()
    box({ onCancel: undefined })
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull()
  })
})
