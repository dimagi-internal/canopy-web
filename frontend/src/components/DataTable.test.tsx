// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { TH_CLASS, Th, type TableDensity } from './DataTable'

function renderTh(ui: ReactNode) {
  render(
    <table>
      <thead>
        <tr>{ui}</tr>
      </thead>
    </table>,
  )
}

afterEach(cleanup)

describe('Th', () => {
  it('defaults to compact', () => {
    renderTh(<Th>Name</Th>)
    expect(screen.getByRole('columnheader', { name: 'Name' }).className).toBe('py-1 font-medium')
  })

  it.each(Object.entries(TH_CLASS))('renders the %s density exactly', (density, classes) => {
    renderTh(<Th density={density as TableDensity}>Col</Th>)
    expect(screen.getByRole('columnheader').className).toBe(classes)
  })

  it('appends extra classes and forwards props', () => {
    renderTh(
      <Th density="roomy" className="text-right" scope="col">
        Cost
      </Th>,
    )
    const th = screen.getByRole('columnheader', { name: 'Cost' })
    expect(th.className).toBe('px-3 py-2 font-medium text-right')
    expect(th.getAttribute('scope')).toBe('col')
  })
})
