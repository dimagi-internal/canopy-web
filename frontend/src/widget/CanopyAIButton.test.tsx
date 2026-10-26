// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setAssistant } from './assistant'
import { CanopyAIButton } from './CanopyAIButton'

afterEach(() => {
  cleanup()
  setAssistant(null)
})

describe('CanopyAIButton', () => {
  it('is absent while the widget is not up', () => {
    render(<CanopyAIButton />)
    expect(screen.queryByTestId('canopy-ai')).toBeNull()
  })

  it('opens and closes the widget the bubble would', () => {
    const toggle = vi.fn()
    render(<CanopyAIButton />)
    act(() => setAssistant({ toggle }))
    fireEvent.click(screen.getByRole('button', { name: 'Canopy AI' }))
    expect(toggle).toHaveBeenCalledTimes(1)
    act(() => setAssistant(null))
    expect(screen.queryByTestId('canopy-ai')).toBeNull()
  })
})
