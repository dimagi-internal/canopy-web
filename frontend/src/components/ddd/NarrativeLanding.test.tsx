// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { DddNarrativeDetail, DddNarrativeVersion } from '@/api/ddd'
import { NarrativeLanding } from './NarrativeLanding'
import { cutRows } from './CutVideoList'

/**
 * A recorded narrative (canopy#796) is several short videos, one per cut. Its
 * narrative page shows each cut's video beside what that cut says, in one
 * place (canopy-web#1293); every other narrative keeps the one-paragraph story.
 */

const detail = vi.hoisted(() => ({ current: null as unknown }))
vi.mock('@/api/ddd', async (orig) => ({
  ...(await orig<object>()),
  getNarrative: vi.fn(async () => detail.current),
}))

const NARRATION = [
  { id: 's1', title: 'Cut 1 · Register — land', text: 'Registrations land on the map.' },
  { id: 's2', title: 'Cut 1 · Register — flag', text: 'A quick form is flagged.' },
  { id: 's3', title: 'Cut 2 · Decide — red flags', text: 'Red flags rule a waterpoint out.' },
  { id: 's4', title: 'Cut 3 · Optional — good rider', text: 'A rider doing it well.' },
]

const cut = (over: Record<string, unknown>) => ({
  cut_id: 'cut1',
  title: 'Cut 1 · Register',
  scene_ids: ['s1', 's2'],
  walkthrough_id: 'w1',
  video_url: '/walkthrough/w1/content?t=a',
  video_viewer_url: '/walkthrough/w1?t=a',
  duration_sec: 34,
  ...over,
})

function version(over: Partial<DddNarrativeVersion>): DddNarrativeVersion {
  return {
    version: 3,
    review_id: 'r3',
    title: 'Chlorine',
    story: 'Registrations land on the map. A quick form is flagged.',
    narration: NARRATION,
    created_at: '2026-10-08T00:00:00Z',
    gate: 'concept_change',
    status: 'pending',
    video_url: '/walkthrough/w1/content?t=a',
    video_viewer_url: '/walkthrough/w1?t=a',
    runs: [],
    ...over,
  }
}

function show(v: DddNarrativeVersion) {
  detail.current = {
    slug: 'chlorine',
    title: 'Chlorine',
    story: v.story,
    phase: null,
    project_slug: null,
    visibility: 'public',
    current_version: { review_id: 'r3', version: 3, title: 'Chlorine', story: v.story, video_url: v.video_url, video_viewer_url: v.video_viewer_url },
    versions: [v],
  } satisfies DddNarrativeDetail
  return render(
    <MemoryRouter>
      <NarrativeLanding slug="chlorine" />
    </MemoryRouter>,
  )
}

afterEach(cleanup)

describe('NarrativeLanding — a recorded narrative', () => {
  it('shows the hero, then every cut beside its own words', async () => {
    const { container } = show(
      version({ cuts: [cut({}), cut({ cut_id: 'cut2', title: 'Cut 2 · Decide', scene_ids: ['s3'], walkthrough_id: 'w2', video_url: '/walkthrough/w2/content', video_viewer_url: '/walkthrough/w2', duration_sec: null })] }),
    )
    const register = await screen.findByRole('region', { name: 'Cut 1 · Register' })
    expect(register.querySelector('video')?.getAttribute('src')).toContain('/walkthrough/w1/content?t=a')
    expect(register.textContent).toContain('Registrations land on the map.')
    expect(register.textContent).toContain('A quick form is flagged.')
    expect(register.textContent).not.toContain('Red flags')
    expect(register.textContent).toContain('0:34')
    expect(register.querySelector('a')?.getAttribute('href')).toContain('/walkthrough/w1?t=a')

    const decide = screen.getByRole('region', { name: 'Cut 2 · Decide' })
    expect(decide.textContent).toContain('Red flags rule a waterpoint out.')

    // A cut the narration names but nobody rendered is listed, not dropped.
    const optional = screen.getByRole('region', { name: 'Cut 3 · Optional' })
    expect(optional.querySelector('video')).toBeNull()
    expect(optional.textContent).toContain('Not rendered yet.')
    expect(optional.textContent).toContain('optional')

    // Hero on top, then one player per rendered cut; the story is not repeated as prose.
    expect(container.querySelectorAll('video')).toHaveLength(3)
    expect(screen.queryByText(/^Registrations land on the map\. A quick form is flagged\.$/)).toBeNull()
  })
})

describe('NarrativeLanding — any other narrative', () => {
  it('keeps the one-paragraph story and a single video', async () => {
    const { container } = show(version({ cuts: [] }))
    expect(await screen.findByText('Registrations land on the map. A quick form is flagged.')).toBeTruthy()
    expect(screen.queryByRole('region', { name: 'Cut 1 · Register' })).toBeNull()
    expect(container.querySelectorAll('video')).toHaveLength(1)
  })
})

describe('cutRows', () => {
  it('orders uploads by their first scene and slots unrendered cuts in place', () => {
    const scenes = NARRATION.map((n) => ({ id: n.id, title: n.title, text: n.text }))
    const rows = cutRows(
      [cut({ cut_id: 'cut2', title: 'Cut 2 · Decide', scene_ids: ['s3'] }), cut({})],
      scenes,
    )
    expect(rows.map((r) => [r.kind, r.title])).toEqual([
      ['video', 'Cut 1 · Register'],
      ['video', 'Cut 2 · Decide'],
      ['missing', 'Cut 3 · Optional'],
    ])
  })

  it('adds nothing for a narrative whose scenes name no cuts', () => {
    expect(cutRows([], [{ id: 'a', title: 'Opening', text: 'x' }])).toEqual([])
  })
})
