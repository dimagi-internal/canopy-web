// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { DeclaredConnections } from './DeclaredConnections'

afterEach(cleanup)

describe('DeclaredConnections', () => {
  it('says nothing for a view that declares none', () => {
    const { container } = render(<DeclaredConnections connections={[]} />)
    expect(container.textContent).toBe('')
  })

  it('names whose account it uses and what it may read and change', () => {
    render(
      <DeclaredConnections
        connections={[
          { provider: 'salesforce', as: 'viewer', reads: [{ method: 'GET', path: 'sobjects/Lead/*' }], writes: [] },
          { provider: 'salesforce', as: 'shared', agent: 'eva', reads: [], writes: [{ method: 'PATCH', path: 'sobjects/Lead/*' }] },
        ]}
      />,
    )
    expect(screen.getByText(/This page can use Salesforce as you/)).toBeTruthy()
    expect(screen.getByText(/This page can use Salesforce as eva/)).toBeTruthy()
    expect(screen.getByText('Read Leads')).toBeTruthy()
    expect(screen.getByText('Change Leads')).toBeTruthy()
  })
})
