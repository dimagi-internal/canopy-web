// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { TH_CLASS, Td, Th, Tr, type TableDensity } from './DataTable'

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

  it('composes alignment, breakpoint, weight and label', () => {
    renderTh(
      <Th density="tight" align="right" hideBelow="md" weight="normal" label>
        Cost
      </Th>,
    )
    expect(screen.getByRole('columnheader').className).toBe(
      'px-2 py-1.5 font-normal text-right text-[11px] uppercase tracking-wide text-muted-foreground hidden md:table-cell',
    )
  })

  it('renders an unpadded, weightless header with no stray classes', () => {
    renderTh(
      <>
        <Th density="none" weight="inherit" srOnly>
          Actions
        </Th>
        <Th density="none" weight="inherit" data-testid="empty" />
      </>,
    )
    expect(screen.getByRole('columnheader', { name: 'Actions' }).className).toBe('sr-only')
    expect(screen.getByTestId('empty').hasAttribute('class')).toBe(false)
  })
})

describe('Td and Tr', () => {
  function renderRow(ui: ReactNode) {
    render(
      <table>
        <tbody>{ui}</tbody>
      </table>,
    )
  }

  it('Td defaults to compact and composes tone, alignment and flags', () => {
    renderRow(
      <tr>
        <Td>plain</Td>
        <Td density="cozy-gap" tone="muted" align="right" numeric nowrap hideBelow="sm" className="extra">
          figure
        </Td>
      </tr>,
    )
    expect(screen.getByRole('cell', { name: 'plain' }).className).toBe('py-1')
    expect(screen.getByRole('cell', { name: 'figure' }).className).toBe(
      'whitespace-nowrap py-1.5 pr-3 text-muted-foreground text-right tabular-nums hidden sm:table-cell extra',
    )
  })

  it('Tr renders the row rule, header text and top alignment', () => {
    renderRow(
      <>
        <Tr rule="bottom-last" top data-testid="a" className="hover:bg-muted/50">
          <Td>x</Td>
        </Tr>
        <Tr head="label" rule="bottom" data-testid="b">
          <Td>y</Td>
        </Tr>
      </>,
    )
    expect(screen.getByTestId('a').className).toBe('border-b border-border last:border-0 align-top hover:bg-muted/50')
    expect(screen.getByTestId('b').className).toBe(
      'border-b border-border text-left text-[11px] uppercase tracking-wide text-muted-foreground',
    )
  })
})
