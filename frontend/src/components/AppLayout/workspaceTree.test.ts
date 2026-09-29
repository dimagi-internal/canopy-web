import { describe, expect, it } from 'vitest'
import { workspaceTree } from './AppLayout'

const ws = (slug: string, parent: string | null = null) => ({ slug, parent })

describe('workspaceTree', () => {
  it('lists each child directly under its parent, indented', () => {
    const rows = workspaceTree([
      ws('connect', 'dimagi'),
      ws('family'),
      ws('dimagi'),
      ws('strategy', 'dimagi'),
      ws('strat-team', 'strategy'),
    ])
    expect(rows.map((r) => [r.w.slug, r.depth])).toEqual([
      ['family', 0],
      ['dimagi', 0],
      ['connect', 1],
      ['strategy', 1],
      ['strat-team', 2],
    ])
  })

  it('treats a workspace whose parent is not visible as a root', () => {
    // A division owner who is not an org member sees only their division.
    const rows = workspaceTree([ws('strategy', 'dimagi')])
    expect(rows).toEqual([{ w: ws('strategy', 'dimagi'), depth: 0 }])
  })
})
