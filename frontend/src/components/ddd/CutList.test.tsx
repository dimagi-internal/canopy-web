// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { CutList, cutSlots, formatDuration, type CutVideo } from './CutList'

afterEach(cleanup)

const scenes = [
  { id: 's1', title: 'Cut 1 · Register — land on the map', narration: 'Register waterpoints.' },
  { id: 's2', title: 'Cut 1 · Register — ward corrected', narration: 'Correct the ward.' },
  { id: 's3', title: 'Cut 2 · Decide — red flags', narration: 'Decide where dispensers go.' },
  { id: 's4', title: 'Cut 3 · Optional — what good looks like', narration: 'A rider doing it well.' },
]

const video = (over: Partial<CutVideo>): CutVideo => ({
  cut_id: 'register',
  title: 'Cut 1 · Register',
  scene_ids: ['s1', 's2'],
  walkthrough_id: 'w1',
  video_url: '/walkthrough/w1/content',
  viewer_url: '/walkthrough/w1',
  duration_sec: 31,
  ...over,
})

describe('cutSlots', () => {
  it('lists every cut the narration names, rendered or not, in narration order', () => {
    // Uploaded out of order, and cut 2 never rendered.
    const slots = cutSlots(
      [
        video({ cut_id: 'good', title: 'good', scene_ids: ['s4'], walkthrough_id: 'w3' }),
        video({}),
      ],
      scenes,
    )
    expect(slots.map((s) => s.title)).toEqual([
      'Cut 1 · Register',
      'Cut 2 · Decide',
      // An upload titled only by its id takes the narration's label.
      'Cut 3 · Optional',
    ])
    expect(slots.map((s) => s.video?.walkthrough_id ?? null)).toEqual(['w1', null, 'w3'])
    expect(slots.map((s) => s.optional)).toEqual([false, false, true])
    expect(slots[0].scenes.map((s) => s.id)).toEqual(['s1', 's2'])
  })

  it('formats lengths as m:ss and hides an unknown one', () => {
    expect(formatDuration(31)).toBe('0:31')
    expect(formatDuration(125.4)).toBe('2:05')
    expect(formatDuration(null)).toBeNull()
    expect(formatDuration(Number.NaN)).toBeNull()
  })
})

describe('CutList', () => {
  it('shows each cut with its video, length, words and own page; a missing one says so', () => {
    render(<CutList cuts={[video({})]} scenes={scenes} hero={{ video_url: '/walkthrough/w1/content' }} />)
    const register = screen.getByRole('region', { name: 'Cut 1 · Register' })
    expect(register.querySelector('video')?.getAttribute('src')).toContain('/walkthrough/w1/content')
    expect(register.textContent).toContain('0:31')
    expect(register.textContent).toContain('Correct the ward.')
    expect(screen.getAllByRole('link', { name: 'Open this video' })[0].getAttribute('href')).toContain(
      '/walkthrough/w1',
    )
    const decide = screen.getByRole('region', { name: 'Cut 2 · Decide' })
    expect(decide.querySelector('video')).toBeNull()
    expect(decide.textContent).toContain('Not rendered yet.')
    expect(screen.getByRole('region', { name: 'Cut 3 · Optional' }).textContent).toContain('Optional')
    // The hero IS cut 1, so it is not shown twice.
    expect(screen.queryByRole('region', { name: 'Full video' })).toBeNull()
    expect(screen.getByText(/3 short videos.*2 not rendered yet/)).toBeTruthy()
  })

  it('puts a separate hero video on top of the cuts', () => {
    const { container } = render(
      <CutList cuts={[video({})]} scenes={scenes.slice(0, 2)} hero={{ video_url: '/walkthrough/w9/content' }} />,
    )
    const videos = container.querySelectorAll('video')
    expect(videos).toHaveLength(2)
    expect(videos[0].getAttribute('src')).toContain('/walkthrough/w9/content')
    expect(screen.getByRole('region', { name: 'Full video' })).toBeTruthy()
  })
})
