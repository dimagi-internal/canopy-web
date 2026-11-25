// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { keepsNativeMenu } from './useMessageFork'

function ev(target: Element, pointerType?: string) {
  return { target, nativeEvent: { pointerType } as unknown as MouseEvent }
}

describe('keepsNativeMenu', () => {
  it('leaves a long-press or right-click on a link to the browser', () => {
    const p = document.createElement('p')
    p.innerHTML = '<a href="https://canopy.dimagi.com/w/dimagi/views/x"><strong>link</strong></a>'
    expect(keepsNativeMenu(ev(p.querySelector('strong')!, 'mouse'))).toBe(true)
  })

  it('leaves any long-press on a phone to the browser, so text can be selected', () => {
    expect(keepsNativeMenu(ev(document.createElement('p'), 'touch'))).toBe(true)
  })

  it('opens the message menu for a mouse right-click on plain text', () => {
    expect(keepsNativeMenu(ev(document.createElement('p'), 'mouse'))).toBe(false)
    expect(keepsNativeMenu(ev(document.createElement('p')))).toBe(false)
  })
})
