// @vitest-environment jsdom
// A private conversation takes no file: a picked one is shown refused, with why, and
// never uploaded (the server refuses it too).
import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const uploadAttachment = vi.fn()
vi.mock('@/api/chat', () => ({ uploadAttachment: (...a: unknown[]) => uploadAttachment(...a), deleteAttachment: vi.fn() }))

const { PRIVATE_NO_FILES, useAttachments } = await import('./useAttachments')

describe('useAttachments in a private conversation', () => {
  it('refuses the file in the chip and uploads nothing', () => {
    const { result } = renderHook(() => useAttachments('s1', PRIVATE_NO_FILES))
    act(() => result.current.attach([new File(['x'], 'amina.png', { type: 'image/png' })]))
    expect(result.current.attachments).toEqual([
      expect.objectContaining({ filename: 'amina.png', uploading: false, error: PRIVATE_NO_FILES })])
    expect(uploadAttachment).not.toHaveBeenCalled()
  })
})
