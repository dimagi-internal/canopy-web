import { afterEach, describe, expect, it } from 'vitest'
import type { Message } from 'canopy-ui/chat'

import type { ChatSessionDetail } from '@/api/chat'
import {
  cachedMessages,
  cachedMeta,
  forgetAll,
  knownNotPrivate,
  rememberListed,
  rememberMessages,
  rememberMeta,
} from './sessionCache'

const detail = (over: Partial<ChatSessionDetail> = {}) =>
  ({ id: 's1', sensitive: false, title: 'T', messages: [{ turn_index: 1 }] , ...over }) as unknown as ChatSessionDetail
const msgs = [{ id: 't1', turn_index: 1, role: 'user', plaintext: 'hi' }] as unknown as Message[]

afterEach(forgetAll)

describe('sessionCache', () => {
  it('knows nothing about a chat it never saw', () => {
    expect(knownNotPrivate('s1')).toBe(false)
    expect(cachedMeta('s1')).toBeNull()
    expect(cachedMessages('s1')).toBeNull()
  })

  it('keeps an ordinary chat: its detail (without the transcript twice) and its messages', () => {
    rememberMeta(detail())
    rememberMessages('s1', msgs)
    expect(knownNotPrivate('s1')).toBe(true)
    expect(cachedMeta('s1')?.title).toBe('T')
    expect(cachedMeta('s1')?.messages).toEqual([])
    expect(cachedMessages('s1')).toBe(msgs)
  })

  it('a list read keeps what an earlier open cached', () => {
    rememberMeta(detail())
    rememberMessages('s1', msgs)
    rememberListed([{ id: 's1', sensitive: false }])
    expect(cachedMessages('s1')).toBe(msgs)
  })

  it('keeps nothing of a private conversation but that it is private', () => {
    rememberMeta(detail({ sensitive: true } as Partial<ChatSessionDetail>))
    rememberMessages('s1', msgs)
    expect(knownNotPrivate('s1')).toBe(false)
    expect(cachedMeta('s1')).toBeNull()
    expect(cachedMessages('s1')).toBeNull()
  })

  it('drops what it held when a list says the chat is private', () => {
    rememberMeta(detail())
    rememberMessages('s1', msgs)
    rememberListed([{ id: 's1', sensitive: true }])
    expect(cachedMeta('s1')).toBeNull()
    expect(cachedMessages('s1')).toBeNull()
  })
})
