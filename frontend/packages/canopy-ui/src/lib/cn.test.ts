import { describe, expect, it } from 'vitest'
import { cn } from './cn'

describe('cn', () => {
  it('merges conditional classes', () => {
    const on = (flag: boolean) => flag
    expect(cn('a', on(false) && 'b', 'c')).toBe('a c')
  })
  it('lets later tailwind classes win', () => {
    expect(cn('px-2', 'px-4')).toBe('px-4')
  })
})
