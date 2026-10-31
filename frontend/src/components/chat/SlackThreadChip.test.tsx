// @vitest-environment jsdom
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { SlackThreadChip } from './SlackThreadChip'

afterEach(() => cleanup())

it('shows the channel and offers the link and Disconnect', async () => {
  const onDetach = vi.fn()
  render(<SlackThreadChip thread={{ channel: '#canopy-support', url: 'https://x.slack.com/a' }} onDetach={onDetach} />)
  const chip = screen.getByTestId('slack-thread-chip')
  expect(chip.textContent).toContain('#canopy-support')
  fireEvent.click(chip)
  await waitFor(() => expect(screen.getByTestId('slack-thread-link')).toBeTruthy())
  fireEvent.click(screen.getByTestId('detach-slack'))
  expect(onDetach).toHaveBeenCalled()
})
