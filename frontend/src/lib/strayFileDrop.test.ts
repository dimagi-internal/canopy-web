// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { installStrayFileDropGuard } from './strayFileDrop'

function dragEvent(type: string, types: string[], handled = false): DragEvent {
  const e = new Event(type, { bubbles: true, cancelable: true }) as DragEvent
  Object.defineProperty(e, 'dataTransfer', { value: { types, dropEffect: 'copy' } })
  if (handled) e.preventDefault()
  return e
}

describe('installStrayFileDropGuard', () => {
  let uninstall: () => void = () => {}
  afterEach(() => uninstall())

  it('cancels a file drop nobody handled, so the file never replaces the page', () => {
    uninstall = installStrayFileDropGuard()
    const over = dragEvent('dragover', ['Files'])
    const drop = dragEvent('drop', ['Files'])
    document.body.dispatchEvent(over)
    document.body.dispatchEvent(drop)
    expect(over.defaultPrevented).toBe(true)
    expect(over.dataTransfer!.dropEffect).toBe('none')
    expect(drop.defaultPrevented).toBe(true)
  })

  it('leaves a handled drop, and a drag carrying no files, alone', () => {
    uninstall = installStrayFileDropGuard()
    const handled = dragEvent('dragover', ['Files'], true)
    document.body.dispatchEvent(handled)
    expect(handled.dataTransfer!.dropEffect).toBe('copy')
    const text = dragEvent('drop', ['text/plain'])
    document.body.dispatchEvent(text)
    expect(text.defaultPrevented).toBe(false)
  })
})
