import { describe, expect, it } from 'vitest'
import type { ThreadMessage } from '@/api/threads'
import { agreed, outOfBudget } from '../huddles/__fixtures__/agreementThreads'
import { audienceOf, budgetWords, messageState, normPosition, POSITION_WORDS, resultOf, said, statusWords } from './threadModel'

describe('threadModel', () => {
  it('says each position in plain words', () => {
    expect(['agree', 'counter', 'decline', 'question'].map((p) => POSITION_WORDS[normPosition(p)!]))
      .toEqual(['Agrees', 'Suggests a change', "Doesn't agree", 'Asks'])
    expect(normPosition('Agreed!')).toBe('agree')
    expect(normPosition('maybe')).toBeNull()
  })

  it('says how a thread ended', () => {
    expect(resultOf(agreed)).toBe('agreed')
    expect(statusWords(agreed)).toBe('Agreed')
    expect(resultOf(outOfBudget)).toBe('not_agreed')
    expect(statusWords(outOfBudget)).toBe('Ran out of messages without agreeing')
    expect(statusWords({ status: 'settled', outcome: { result: 'not_agreed' } })).toBe("Didn't agree")
    expect(statusWords({ status: 'timed_out', outcome: {} })).toBe('Ran out of time without agreeing')
    expect(resultOf({ status: 'open', outcome: {} })).toBeNull()
    expect(budgetWords(agreed)).toBe('2 of 4 messages used')
  })

  it('reads a message: who it is to, what it says, its state', () => {
    const [one] = agreed.messages
    expect(audienceOf(agreed, 'eva')).toEqual(['echo'])
    expect(said(one)).toMatchObject({ position: 'counter', proposal: { title: expect.stringMatching(/^IDM talk/) } })
    expect(messageState(one)).toBe('replied')
    const base = { ...one, block: null } as ThreadMessage
    expect(messageState({ ...base, status: 'running' })).toBe('waiting')
    expect(messageState({ ...base, status: 'failed' })).toBe('failed')
    expect(messageState({ ...base, status: 'done' })).toBe('no-reply')
    expect(messageState({ ...base, content_hidden: true })).toBe('hidden')
  })
})
