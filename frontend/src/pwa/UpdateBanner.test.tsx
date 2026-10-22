// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { UpdateBanner } from './UpdateBanner'
import { markUpdateReady } from './updateReady'

afterEach(() => {
  cleanup()
})

describe('UpdateBanner', () => {
  it('appears once a build is waiting, and Reload switches to it', () => {
    render(<UpdateBanner />)
    const apply = vi.fn()
    act(() => markUpdateReady(apply))
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }))
    expect(apply).toHaveBeenCalledOnce()
  })

  it('stays dismissed until the next deploy', () => {
    render(<UpdateBanner />)
    act(() => markUpdateReady(vi.fn()))
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('update-banner')).toBeNull()
    act(() => markUpdateReady(vi.fn()))
    expect(screen.getByTestId('update-banner')).toBeTruthy()
  })
})
