// Creating an agent from the browser, and what is left before it can work.
//
// Three calls, each safe to repeat (apps/agents/creation.py): check the repo,
// push the factory's scaffold into it, then register through the SAME upsert the
// plugin uses (`POST /api/agents/`). The checklist is `GET …/setup`, computed
// server-side from the system's own checks so this page never keeps a copy.
import { apiV2 } from './client.v2'
import { problemMessage } from './problem'
import type { components } from './generated'

type Schemas = components['schemas']
export type RepoCheckOut = Schemas['RepoCheckOut']
export type AgentScaffoldOut = Schemas['AgentScaffoldOut']
export type AgentSetupStepOut = Schemas['AgentSetupStepOut']

/** A refusal the person can fix, with where to fix it when the server knows. */
export class CreationError extends Error {
  fixUrl: string
  constructor(message: string, fixUrl = '') {
    super(message)
    this.fixUrl = fixUrl
  }
}

function fail(error: unknown, fallback: string): never {
  const extras = (error as { extras?: { fix_url?: unknown } } | undefined)?.extras
  const fixUrl = typeof extras?.fix_url === 'string' ? extras.fix_url : ''
  throw new CreationError(problemMessage(error, fallback), fixUrl)
}

export async function checkScaffoldRepo(workspace: string, slug: string, repo: string): Promise<RepoCheckOut> {
  const res = await apiV2.GET('/api/agents/scaffold/check', {
    params: { query: { workspace, slug, repo } },
  })
  if (!res.response.ok || !res.data) fail(res.error, 'Could not check the repository.')
  return res.data
}

export interface NewAgent {
  workspace: string
  slug: string
  name: string
  mandate: string
  mailbox: string
  stakeholders: string
  repo: string
}

export async function scaffoldAgent(body: NewAgent): Promise<AgentScaffoldOut> {
  const res = await apiV2.POST('/api/agents/scaffold', { body })
  if (!res.response.ok || !res.data) fail(res.error, 'Could not push the scaffold.')
  return res.data
}

/** Register it — the ordinary upsert, so the web and `/canopy:create-agent` share one door. */
export async function registerAgent(agent: NewAgent, pushed: AgentScaffoldOut): Promise<void> {
  const res = await apiV2.POST('/api/agents/', {
    body: {
      slug: agent.slug,
      name: agent.name,
      description: agent.mandate,
      persona: '',
      email: agent.mailbox,
      avatar_url: '',
      workspace: agent.workspace,
      repo_url: pushed.repo_url,
      repo_ref: pushed.repo_ref,
    },
  })
  if (!res.response.ok) fail(res.error, 'Could not register the agent.')
}

export async function getAgentSetup(slug: string): Promise<AgentSetupStepOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/setup', { params: { path: { slug } } })
  if (!res.response.ok || !res.data) fail(res.error, 'Could not read what is left to set up.')
  // --immutable codegen types an array response as a non-iterable readonly shape
  // (see listGitHubInstallations in api/github.ts for the same cast).
  return res.data as unknown as AgentSetupStepOut[]
}
