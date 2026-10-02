import { describe, expect, it } from 'vitest'

import type { AgentEdgeOut, AgentTopologyAgentOut } from '@/api/workspaces'
import { explainEdge, grantable } from './agentTopology'

const edge = (over: Partial<AgentEdgeOut>): AgentEdgeOut => ({
  source: 'ada', target: 'hal', access: 'confined', basis: 'capabilities', capabilities: ['ask'],
  full_rule: null, explicit_admin: false, can_grant: true, can_revoke: false, ...over,
})
const agent = (slug: string, login: string | null): AgentTopologyAgentOut => ({
  slug, name: slug[0].toUpperCase() + slug.slice(1), workspace: 'connect', owner_email: null,
  login_email: login, login_user_id: login ? 1 : null, interface_published: true, full_people: [],
})

describe('explainEdge', () => {
  it('names the capability a confined sender is held to', () => {
    expect(explainEdge(edge({}), agent('ada', 'ada@x'), agent('hal', null))).toContain('confines members to ask')
  })
  it('says what to do when the login is outside the workspace', () => {
    const e = edge({ access: 'none', basis: 'not-member', can_grant: false })
    expect(explainEdge(e, agent('eva', 'eva@x'), agent('hal', null))).toContain('Add it to connect first')
  })
})

describe('grantable', () => {
  it('skips edges already full and ones the viewer cannot grant', () => {
    const list = [edge({}), edge({ access: 'full', basis: 'no-interface' }), edge({ can_grant: false })]
    expect(grantable(list)).toHaveLength(1)
  })
})
