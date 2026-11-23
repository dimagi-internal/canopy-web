// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const { ReplyPushEditor } = await import('./hostSites/HostGrantEditor')

afterEach(cleanup)

describe('ReplyPushEditor', () => {
  it('saves the webhook URL and mode only once something changed', () => {
    const onSave = vi.fn()
    render(<ReplyPushEditor app={{ reply_webhook_url: null, reply_mode: 'final' }} busy={false} onSave={onSave} />)
    expect(screen.getByText(/No webhook set here/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    fireEvent.change(screen.getByLabelText('Reply webhook URL'), {
      target: { value: ' https://labs.test/messaging/canopy/replies/ ' },
    })
    fireEvent.change(screen.getByLabelText('Reply mode'), { target: { value: 'stream' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(onSave).toHaveBeenCalledWith('https://labs.test/messaging/canopy/replies/', 'stream')
    expect(screen.getByText(/asks for your passkey/)).toBeTruthy()
  })

  it('says where replies go when the site has a webhook', () => {
    render(
      <ReplyPushEditor
        app={{ reply_webhook_url: 'https://labs.test/r/', reply_mode: 'stream', reply_webhook_set_by: 'jj@dimagi.com' }}
        busy={false}
        onSave={() => {}}
      />,
    )
    expect(screen.getByText(/Set by jj@dimagi.com: canopy pushes the agent's replies/)).toBeTruthy()
    expect((screen.getByLabelText('Reply mode') as HTMLSelectElement).value).toBe('stream')
  })
})
