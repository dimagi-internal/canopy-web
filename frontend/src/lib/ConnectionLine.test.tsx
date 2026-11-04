// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ConnectionLine } from './ConnectionLine'

afterEach(cleanup)

describe('ConnectionLine', () => {
  it('shows a call it does not recognise in a monospace span', () => {
    render(<ConnectionLine line={{ words: 'Other Salesforce access:', code: 'GET limits' }} />)
    expect(screen.getByText('GET limits').className).toContain('font-mono')
    expect(screen.getByText(/Other Salesforce access:/)).toBeTruthy()
  })

  it('shows plain words plainly', () => {
    const { container } = render(<ConnectionLine line={{ words: 'Read Leads' }} />)
    expect(container.textContent).toBe('Read Leads')
    expect(container.querySelector('.font-mono')).toBeNull()
  })
})
