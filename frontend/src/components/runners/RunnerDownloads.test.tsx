// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const getRunnerDownloads = vi.fn()

class FakeRunnerPairingError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

vi.mock('@/api/runnerPairings', () => ({ getRunnerDownloads, RunnerPairingError: FakeRunnerPairingError }))

const { RunnerDownloads } = await import('./RunnerDownloads')

const DEV = '~/emdash-projects/canopy-web/runner/canopy_runner/scripts/install-runner.sh --workspace connect'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerDownloads', () => {
  it('lists each built installer, and says honestly which are unsigned', async () => {
    getRunnerDownloads.mockResolvedValue({
      sha: 'a'.repeat(40),
      downloads: [
        { platform: 'macos-arm64', label: 'macOS (Apple silicon)', file: 'a.dmg', size: 104857600, signed: true,
          notarized: true, href: '/download/runner/macos-arm64' },
        { platform: 'windows-x86_64', label: 'Windows', file: 'b.exe', size: 0, signed: false, notarized: false,
          href: '/download/runner/windows-x86_64' },
      ],
    })
    render(<RunnerDownloads authed developerCommand={DEV} />)
    const rows = await screen.findAllByTestId('runner-download')
    expect(rows.map((r) => r.querySelector('td')?.textContent)).toEqual(['macOS (Apple silicon)', 'Windows'])
    expect(rows[0].textContent).toContain('100 MB')
    expect(rows[0].textContent).toContain('Signed')
    expect(rows[1].textContent).toMatch(/Unsigned build: Windows SmartScreen will warn — choose More info → Run anyway/)
    const links = screen.getAllByRole('link', { name: 'Download' }).map((a) => a.getAttribute('href'))
    expect(links).toEqual(['/download/runner/macos-arm64', '/download/runner/windows-x86_64'])
    expect(screen.getByText('Developers')).toBeTruthy()
    expect(screen.getByText(DEV)).toBeTruthy()
  })

  it('says when nothing is built', async () => {
    getRunnerDownloads.mockResolvedValue({ sha: '', downloads: [] })
    render(<RunnerDownloads authed developerCommand={DEV} />)
    expect(await screen.findByText(/No installer has been built yet/)).toBeTruthy()
  })

  it('signed out, offers sign-in instead of asking', () => {
    render(<RunnerDownloads authed={false} developerCommand={DEV} />)
    expect(getRunnerDownloads).not.toHaveBeenCalled()
    expect(screen.getByRole('link', { name: 'Sign in to download' }).getAttribute('href')).toContain(
      '/accounts/google/login/?next=',
    )
  })

  it('a 401 is treated as signed out', async () => {
    getRunnerDownloads.mockRejectedValue(new FakeRunnerPairingError(401, 'Authentication required'))
    render(<RunnerDownloads authed developerCommand={DEV} />)
    expect(await screen.findByRole('link', { name: 'Sign in to download' })).toBeTruthy()
  })
})
