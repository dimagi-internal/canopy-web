// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import StoryboardsPage from './StoryboardsPage'
import * as api from '@/api/storyboards'

vi.mock('@/api/storyboards', async () => {
  const actual = await vi.importActual<typeof api>('@/api/storyboards')
  return { ...actual, listStoryboards: vi.fn() }
})

const listStoryboards = api.listStoryboards as unknown as ReturnType<typeof vi.fn>

function item(over: Partial<api.StoryboardListItem> = {}): api.StoryboardListItem {
  return {
    slug: 'oes-supply',
    title: 'From the appropriation to the child',
    lede: 'Four countries, one therapeutic-food supply base.',
    capability: 'suggest',
    layout: 'review',
    act_count: 5,
    share_url: 'https://labs.example/canopy/storyboard/oes-supply?t=abc',
    ...over,
  }
}

function mount() {
  return render(
    <MemoryRouter>
      <StoryboardsPage />
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('StoryboardsPage', () => {
  it('lists every board with its layout, and links into the board itself', async () => {
    listStoryboards.mockResolvedValue({
      items: [item(), item({ slug: 'rf-surveys-quick', title: 'Impact evaluation on Connect', layout: 'reel', act_count: 1, capability: 'read' })],
    })
    mount()
    const first = await screen.findByRole('link', { name: 'From the appropriation to the child' })
    expect(first.getAttribute('href')).toBe('/storyboard/oes-supply')
    expect(screen.getByText('Review')).toBeTruthy()
    expect(screen.getByText('Reel')).toBeTruthy()
    expect(screen.getByText(/5 acts/)).toBeTruthy()
    expect(screen.getByText(/1 act ·/)).toBeTruthy()
  })

  it('offers the share link only when the API returned one', async () => {
    listStoryboards.mockResolvedValue({ items: [item(), item({ slug: 'no-link', title: 'Unshared', share_url: null })] })
    mount()
    await screen.findByText('Unshared')
    expect(screen.getAllByRole('button', { name: /Copy the share link/ })).toHaveLength(1)
  })

  it('says so when there is nothing yet', async () => {
    listStoryboards.mockResolvedValue({ items: [] })
    mount()
    await waitFor(() => expect(screen.getByText(/No storyboards yet/)).toBeTruthy())
  })

  it('reports a failed load rather than an empty list', async () => {
    listStoryboards.mockRejectedValue(new Error('Request failed (401)'))
    mount()
    await waitFor(() => expect(screen.getByText(/Couldn’t load storyboards: Request failed \(401\)/)).toBeTruthy())
  })
})
