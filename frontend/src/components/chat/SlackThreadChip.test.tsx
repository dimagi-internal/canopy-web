// @vitest-environment jsdom
import { cleanup, render, screen, fireEvent } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { SlackThreadChip } from './SlackThreadChip'

afterEach(() => cleanup())

it('names the channel, not its id, and opens the thread in the Slack app', () => {
  const assign = vi.fn()
  vi.stubGlobal('location', { ...window.location, assign })
  const app_url = 'slack://channel?team=T1&id=C0123'
  render(<SlackThreadChip thread={{ channel: 'C0123', channel_name: 'canopy-support', app_url, url: 'https://x.slack.com/a' }} />)
  const chip = screen.getByTestId('slack-thread-chip')
  expect(chip.textContent).toBe('Slack · #canopy-support')
  fireEvent.click(chip)
  expect(assign).toHaveBeenCalledWith(app_url)
  vi.unstubAllGlobals()
})

it('never shows the id when the name is not known yet', () => {
  render(<SlackThreadChip thread={{ channel: 'C0123' }} />)
  expect(screen.getByTestId('slack-thread-chip').textContent).toBe('Slack · thread')
})
