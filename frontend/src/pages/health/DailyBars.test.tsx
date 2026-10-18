// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { AnalyticsDayOut } from '@/api/workspaces'
import { DailyBars } from './DailyBars'

afterEach(() => cleanup())

const day = (d: string, turns: number, people: number): AnalyticsDayOut => ({
  day: d, turns, sessions: 0, messages: 0, active_users: people, done: 0, failed: 0, lost: 0, cancelled: 0,
  input_tokens: 0, output_tokens: 0, cache_read_tokens: 0,
})
const DAYS = [day('2026-10-16', 4, 2), day('2026-10-17', 3, 2), day('2026-10-18', 0, 0)]

describe('DailyBars', () => {
  it('captions the whole range, not today’s partial day, until a day is hovered', () => {
    render(<DailyBars title="Turns" days={DAYS} layers={[{ key: 'turns', label: 'Turns', className: 'fill-primary' }]} />)
    expect(screen.getByText('3 days: 7')).toBeTruthy()
  })

  it('uses the server’s range figure when days do not add up to it (distinct people)', () => {
    render(<DailyBars title="People" days={DAYS} rangeTotal={2}
                      layers={[{ key: 'active_users', label: 'People', className: 'fill-primary' }]} />)
    expect(screen.getByText('3 days: 2')).toBeTruthy()
  })
})
