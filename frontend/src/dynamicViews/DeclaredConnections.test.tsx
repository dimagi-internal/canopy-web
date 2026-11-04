// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { DeclaredConnections } from './DeclaredConnections'

afterEach(cleanup)

describe('DeclaredConnections', () => {
  it('says nothing for a screen that declares none', () => {
    const { container } = render(<DeclaredConnections connections={[]} />)
    expect(container.textContent).toBe('')
  })

  it('says whose account it uses and what it may do, writes first', () => {
    const { container } = render(
      <DeclaredConnections
        connections={[
          { provider: 'salesforce', as: 'viewer', reads: [{ method: 'GET', path: 'query' }], writes: [{ method: 'DELETE', path: 'sobjects/Lead/*' }] },
          { provider: 'salesforce', as: 'shared', agent: 'eva', reads: [{ method: 'GET', path: 'query' }], writes: [{ method: 'PATCH', path: 'sobjects/Lead/*' }] },
        ]}
      />,
    )
    expect(container.textContent).toContain('This screen can use your Salesforce: Delete Leads, Search any record you can see')
    expect(container.textContent).toContain('This screen acts as eva in Salesforce: Change Leads, Search any record eva can see')
  })

  it('shows an unrecognised call as code', () => {
    render(<DeclaredConnections connections={[{ provider: 'salesforce', reads: [{ method: 'GET', path: 'limits' }] }]} />)
    expect(screen.getByText('GET limits').className).toContain('font-mono')
  })
})
