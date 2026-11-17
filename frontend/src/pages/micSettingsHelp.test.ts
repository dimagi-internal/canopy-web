// Where to turn the microphone back on names the phone and browser the page is open in.
import { describe, expect, it } from 'vitest'
import { micSettingsHelp } from './ListenPage'

const ANDROID_CHROME = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36'
const SAMSUNG = 'Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/26.0 Chrome/122.0.0.0 Mobile Safari/537.36'
const ANDROID_FIREFOX = 'Mozilla/5.0 (Android 14; Mobile; rv:132.0) Gecko/132.0 Firefox/132.0'
const IPHONE_SAFARI = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1'
const IPHONE_CHROME = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/131.0.6778.73 Mobile/15E148 Safari/604.1'
const MAC_CHROME = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'

describe('micSettingsHelp', () => {
  it('gives Android Chrome the site setting, then the phone’s switch for Chrome', () => {
    const help = micSettingsHelp(ANDROID_CHROME)
    expect(help).toContain('On Android in Chrome')
    expect(help).toContain('Permissions → Microphone → Allow')
    expect(help).toContain('Settings → Apps → Chrome → Permissions → Microphone')
    expect(help).not.toContain('iPhone')
  })

  it('names the Android browser actually in use', () => {
    expect(micSettingsHelp(SAMSUNG)).toContain('Apps → Samsung Internet')
    expect(micSettingsHelp(ANDROID_FIREFOX)).toContain('Apps → Firefox')
  })

  it('gives iPhone Safari aA → Website Settings, and iPhone Chrome the phone’s Settings', () => {
    expect(micSettingsHelp(IPHONE_SAFARI)).toContain('aA in the address bar')
    expect(micSettingsHelp(IPHONE_CHROME)).toContain('Settings → Apps → Chrome')
    expect(micSettingsHelp(IPHONE_CHROME)).not.toContain('aA')
  })

  it('falls back to the address-bar icon on a computer', () => {
    expect(micSettingsHelp(MAC_CHROME)).toContain('icon left of the address bar')
  })
})
