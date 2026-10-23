// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { ListTitle, listTitle, privateTitle } from './privateConversation'

afterEach(cleanup)

describe('a private conversation in a list', () => {
  it('is named by what it is and who started it, never an empty or untitled title', () => {
    expect(privateTitle({ sensitive: true, started_by_name: 'Ann' })).toBe('Private conversation · Ann')
    expect(listTitle({ sensitive: true, started_by_name: '' }, '(untitled)')).toBe('Private conversation')
    expect(listTitle({ sensitive: false }, 'Fix the build')).toBe('Fix the build')
  })

  it('draws a lock', () => {
    render(<ListTitle s={{ sensitive: true, started_by_name: 'Ann' }} fallback="(untitled)" />)
    expect(screen.getByTestId('private-mark')).toBeTruthy()
    expect(screen.getByTestId('private-title').textContent).toBe('Private conversation · Ann')
    cleanup()
    render(<ListTitle s={{ sensitive: false }} fallback="Fix the build" />)
    expect(screen.queryByTestId('private-mark')).toBeNull()
  })
})
