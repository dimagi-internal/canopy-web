// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { DddNarrativeDetail, DddNarrativeVersion } from '@/api/ddd'
import { NarrativeLanding } from './NarrativeLanding'

/** The narrative page shows a recorded narrative as its cuts, and keeps the
 *  one-story, one-video layout for everything else (canopy-web#1293). */

const narrative = vi.hoisted(() => ({ current: null as unknown }))
vi.mock('@/api/ddd', async (orig) => ({
  ...(await orig<object>()),
  getNarrative: vi.fn(async () => narrative.current),
}))

afterEach(cleanup)

const narration = [
  { scene: 1, id: 's1', title: 'Cut 1 · Register — land on the map', text: 'Register waterpoints.' },
  { scene: 2, id: 's2', title: 'Cut 2 · Decide — red flags', text: 'Decide where dispensers go.' },
]

const version = (over: Partial<DddNarrativeVersion> = {}): DddNarrativeVersion => ({
  version: 2,
  review_id: 'r2',
  title: 'Chlorine Dispenser Walkthroughs',
  story: 'Register waterpoints. Decide where dispensers go.',
  narration,
  created_at: '2026-10-07T00:00:00Z',
  gate: 'concept_change',
  status: 'pending',
  video_url: '/walkthrough/w1/content',
  video_viewer_url: '/walkthrough/w1',
  cuts: [],
  runs: [],
  ...over,
})

const detail = (versions: DddNarrativeVersion[]): DddNarrativeDetail =>
  ({
    slug: 'chlorine',
    title: 'Chlorine Dispenser Walkthroughs',
    story: null,
    phase: null,
    project_slug: null,
    visibility: 'private',
    current_version: { review_id: 'r2', version: 2, title: null, story: null, video_url: null, video_viewer_url: null },
    versions,
  }) as DddNarrativeDetail

const renderPage = () =>
  render(
    <MemoryRouter>
      <NarrativeLanding slug="chlorine" />
    </MemoryRouter>,
  )

describe('NarrativeLanding', () => {
  it('lays a recorded narrative out as its cuts, each beside its words', async () => {
    narrative.current = detail([
      version({
        cuts: [
          {
            cut_id: 'register',
            title: 'Cut 1 · Register',
            scene_ids: ['s1'],
            walkthrough_id: 'w1',
            video_url: '/walkthrough/w1/content',
            video_viewer_url: '/walkthrough/w1',
            duration_sec: 28,
          },
        ],
      }),
    ])
    const { container } = renderPage()
    const register = await screen.findByRole('region', { name: 'Cut 1 · Register' })
    expect(register.textContent).toContain('Register waterpoints.')
    expect(register.textContent).toContain('0:28')
    expect(screen.getByRole('link', { name: 'Open this video' }).getAttribute('href')).toContain('/walkthrough/w1')
    expect(screen.getByRole('region', { name: 'Cut 2 · Decide' }).textContent).toContain('Not rendered yet.')
    // The hero is cut 1: one player, not two.
    expect(container.querySelectorAll('video')).toHaveLength(1)
    // The one-block story is replaced by the per-cut words.
    expect(screen.queryByText('Register waterpoints. Decide where dispensers go.')).toBeNull()
  })

  it('keeps one story and one video for a narrative without cuts', async () => {
    narrative.current = detail([version()])
    const { container } = renderPage()
    expect(await screen.findByText('Register waterpoints. Decide where dispensers go.')).toBeTruthy()
    expect(container.querySelectorAll('video')).toHaveLength(1)
    expect(screen.queryByRole('region', { name: 'Cut 1 · Register' })).toBeNull()
    expect(screen.queryByText(/short video/)).toBeNull()
  })

  it('shows an older version its own cuts when expanded', async () => {
    narrative.current = detail([
      version(),
      version({
        version: 1,
        review_id: 'r1',
        cuts: [
          {
            cut_id: 'decide',
            title: 'Cut 2 · Decide',
            scene_ids: ['s2'],
            walkthrough_id: 'w0',
            video_url: '/walkthrough/w0/content',
            video_viewer_url: '/walkthrough/w0',
          },
        ],
      }),
    ])
    renderPage()
    await screen.findByText('Register waterpoints. Decide where dispensers go.')
    expect(screen.queryByRole('region', { name: 'Cut 2 · Decide' })).toBeNull()
    screen.getByText('v1').click()
    expect(await screen.findByRole('region', { name: 'Cut 2 · Decide' })).toBeTruthy()
  })
})
