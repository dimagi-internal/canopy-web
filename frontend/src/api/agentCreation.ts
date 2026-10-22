// Creating an agent from the browser, and what is left before it can work.
//
// Each call is safe to repeat (apps/agents/creation.py): check the repo, push
// the factory's scaffold into it with the person's token, register through the
// SAME upsert the plugin uses (`POST /api/agents/`), then lend the agent that
// same token through the existing `PUT …/github` (`setAgentGitHub`). The checklist is `GET …/setup`, computed
// server-side from the system's own checks so this page never keeps a copy.
import { apiV2 } from './client.v2'
import { ApiError } from './problem'
import type { components } from './generated'

type Schemas = components['schemas']
export type RepoCheckOut = Schemas['RepoCheckOut']
export type AgentScaffoldOut = Schemas['AgentScaffoldOut']
export type AgentSetupStepOut = Schemas['AgentSetupStepOut']

/** A refusal the person can fix; the server's message says how. The shared
 *  `ApiError` (./problem) under its old name. */
export { ApiError as CreationError }

function fail(res: { response: Response; error?: unknown }, fallback: string): never {
  throw ApiError.from(res.response.status, res.error, fallback)
}

/** POST only to keep the token out of a URL; it writes nothing. An empty token
 *  still answers, with the links to make the repo and the token. */
export async function checkScaffoldRepo(
  workspace: string,
  slug: string,
  repo: string,
  token: string,
): Promise<RepoCheckOut> {
  const res = await apiV2.POST('/api/agents/scaffold/check', { body: { workspace, slug, repo, token } })
  if (!res.response.ok || !res.data) fail(res, 'Could not check the repository.')
  return res.data
}

export interface NewAgent {
  workspace: string
  slug: string
  name: string
  mandate: string
  mailbox: string
  /** Its Google login (Drive, Docs, gog) when it is not `mailbox`, e.g. beside a canopy-hosted address. */
  google_account?: string
  stakeholders: string
  repo: string
  /** The person's fine-grained token: pushes the scaffold, then becomes the agent's GitHub identity. */
  token: string
}

export async function scaffoldAgent(body: NewAgent): Promise<AgentScaffoldOut> {
  const res = await apiV2.POST('/api/agents/scaffold', { body: { ...body, google_account: body.google_account ?? '' } })
  if (!res.response.ok || !res.data) fail(res, 'Could not push the scaffold.')
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
      ...(agent.google_account ? { google_account: agent.google_account } : {}),
      avatar_url: '',
      workspace: agent.workspace,
      repo_url: pushed.repo_url,
      repo_ref: pushed.repo_ref,
    },
  })
  if (!res.response.ok) fail(res, 'Could not register the agent.')
}

export async function getAgentSetup(slug: string): Promise<AgentSetupStepOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/setup', { params: { path: { slug } } })
  if (!res.response.ok || !res.data) fail(res, 'Could not read what is left to set up.')
  // --immutable codegen types an array response as a non-iterable readonly shape
  // (see listGitHubInstallations in api/github.ts for the same cast).
  return res.data as unknown as AgentSetupStepOut[]
}
