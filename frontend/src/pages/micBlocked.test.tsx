// @vitest-environment jsdom
// A blocked microphone: the browser's own allow button where it has one, and steps that
// name the phone, the browser, and whether this is the installed app.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { MicBlocked, micSettingsHelp } from './ListenPage'

const ANDROID_CHROME = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36'
const SAMSUNG = 'Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/26.0 Chrome/122.0.0.0 Mobile Safari/537.36'
const ANDROID_FIREFOX = 'Mozilla/5.0 (Android 14; Mobile; rv:132.0) Gecko/132.0 Firefox/132.0'
const IPHONE_SAFARI = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1'
const IPHONE_CHROME = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/131.0.6778.73 Mobile/15E148 Safari/604.1'
const MAC_CHROME = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'

afterEach(() => {
  cleanup()
  delete (window as unknown as Record<string, unknown>).HTMLUserMediaElement
})

describe('micSettingsHelp', () => {
  it('gives Android Chrome the site setting, then the phone’s switch for Chrome', () => {
    const help = micSettingsHelp(ANDROID_CHROME, false)
    expect(help).toContain('On Android in Chrome')
    expect(help).toContain('Permissions → Microphone → Allow')
    expect(help).toContain('Settings → Apps → Chrome → Permissions → Microphone')
    expect(help).not.toContain('iPhone')
  })

  it('sends the installed Android app to the site in Chrome, since it has no address bar', () => {
    const help = micSettingsHelp(ANDROID_CHROME, true, 'canopy.example.com')
    expect(help).toContain('no address bar')
    expect(help).toContain('open canopy.example.com in Chrome')
    expect(help).toContain('Settings → Apps → Chrome')
  })

  it('names the Android browser actually in use', () => {
    expect(micSettingsHelp(SAMSUNG, false)).toContain('Apps → Samsung Internet')
    expect(micSettingsHelp(ANDROID_FIREFOX, false)).toContain('Apps → Firefox')
  })

  it('gives iPhone Safari aA → Website Settings, and iPhone Chrome the phone’s Settings', () => {
    expect(micSettingsHelp(IPHONE_SAFARI, false)).toContain('aA in the address bar')
    expect(micSettingsHelp(IPHONE_CHROME, false)).toContain('Settings → Apps → Chrome')
    expect(micSettingsHelp(IPHONE_CHROME, false)).not.toContain('aA')
    expect(micSettingsHelp(IPHONE_SAFARI, true)).not.toContain('aA')
  })

  it('falls back to the address-bar icon on a computer', () => {
    expect(micSettingsHelp(MAC_CHROME, false)).toContain('icon left of the address bar')
  })
})

describe('MicBlocked', () => {
  it('shows only the steps where the browser has no allow button', () => {
    const { container } = render(<MicBlocked />)
    expect(container.querySelector('usermedia')).toBeNull()
    expect(screen.getByText(/can’t ask again/)).toBeTruthy()
  })

  it('offers the browser’s allow button, closes the stream it hands over, and goes on', () => {
    ;(window as unknown as Record<string, unknown>).HTMLUserMediaElement = class {}
    const onAllowed = vi.fn()
    const { container } = render(<MicBlocked onAllowed={onAllowed} />)
    const el = container.querySelector('usermedia') as HTMLElement
    expect(el).toBeTruthy()
    const stop = vi.fn()
    Object.defineProperty(el, 'stream', { value: { getTracks: () => [{ stop }] } })
    act(() => { el.dispatchEvent(new Event('stream')) })
    expect(stop).toHaveBeenCalledTimes(1)
    expect(onAllowed).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/If that doesn’t work/)).toBeTruthy()
  })
})
