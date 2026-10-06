// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { BlockView } from './BlockView'
import type { Arc } from './huddleModel'

afterEach(cleanup)

const resolutions = {
  huddle: 'h', round: 4, member: 'eva',
  resolutions: [
    { title: 'Pipeline sheet', lead: 'eva', resolution: 'accept', note: 'weekly is fine',
      proposal: { title: 'Pipeline sheet', lead: 'eva', with: ['hal'], why: 'weekly cadence now', plan: ['build weekly'] } },
    { title: 'Funder map', lead: 'eva', resolution: 'reject', note: 'needs to stay daily' },
  ],
}

const arc = (title: string, state: Arc['state']): Arc => ({
  key: `hal|eva|${title}`, title, lead: 'eva', partner: 'hal', state, note: '', from: 'hal-3', to: 'eva-2',
})

it('renders round-4 resolutions: pill, title, note, and the revised proposal on accept', () => {
  const { container } = render(
    <BlockView block={resolutions} member="eva" arcs={[arc('Pipeline sheet', 'amend-accepted'), arc('Funder map', 'amend-rejected')]} />,
  )
  expect(screen.queryByText(/"resolutions"/)).toBeNull()

  const accept = container.querySelector('[data-resolution="accept"]') as HTMLElement
  expect(accept.textContent).toContain('accept')
  expect(accept.textContent).toContain('Pipeline sheet')
  expect(accept.textContent).toContain('weekly is fine')
  expect(accept.querySelector('.text-success')).toBeTruthy()
  // The revised proposal renders as a proposal card — but not as an arc anchor.
  const revised = accept.querySelector('[data-proposal="revised"]') as HTMLElement
  expect(revised.textContent).toContain('weekly cadence now')
  expect(revised.getAttribute('data-anchor')).toBeNull()
  expect(revised.querySelector('[data-partner-state="amend-accepted"]')?.textContent).toContain('amend accepted')

  const reject = container.querySelector('[data-resolution="reject"]') as HTMLElement
  expect(reject.textContent).toContain('reject')
  expect(reject.textContent).toContain('Funder map')
  expect(reject.textContent).toContain('needs to stay daily')
  expect(reject.querySelector('.text-destructive')).toBeTruthy()
  expect(reject.querySelector('[data-proposal]')).toBeNull()
})

it("shows an accepted amend on the lead's original proposal card", () => {
  const { container } = render(
    <BlockView block={{ proposals: [{ title: 'Pipeline sheet', lead: 'eva', with: ['hal'] }] }} member="eva"
      arcs={[arc('Pipeline sheet', 'amend-accepted')]} />,
  )
  const pill = container.querySelector('[data-answer="amend-accepted"]') as HTMLElement
  expect(pill.textContent).toBe('amend accepted')
  expect(pill.className).toContain('text-success')
})
