import { describe, expect, it } from 'vitest'
import { groupScenesByCut } from './CutVideoList'

describe('groupScenesByCut', () => {
  it('groups consecutive scenes by the cut named before the em dash', () => {
    const g = groupScenesByCut([
      { title: 'Cut 1 · Register — a' },
      { title: 'Cut 1 · Register — b' },
      { title: 'Cut 2 · Decide — c' },
    ])
    expect(g.map((x) => [x.cut, x.scenes.length])).toEqual([
      ['Cut 1 · Register', 2],
      ['Cut 2 · Decide', 1],
    ])
  })

  it('keeps a narrative without cuts as one paragraph', () => {
    const g = groupScenesByCut([{ title: 'Opening' }, { title: 'Payoff' }])
    expect(g).toHaveLength(1)
    expect(g[0].cut).toBeNull()
  })
})
