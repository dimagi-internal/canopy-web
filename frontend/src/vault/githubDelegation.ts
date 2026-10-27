// Delegating your GitHub identity to an agent, without canopy-web ever seeing the token.
//
// This browser does what the server used to: it asks GitHub who the token is and whether
// it can push to and open a pull request on the agent's repo (api.github.com answers
// cross-origin requests that carry a bearer token), then seals the token (`zks1`) to the
// recipients canopy-web lists — the machines that run the agent, and your own vault key —
// and sends only the ciphertext and what it found (`PUT /api/agents/{slug}/github`).
//
// The probes create nothing: GitHub checks the token's permission before it validates the
// branch or commit named, so 422 means "allowed" and 403/404 mean "not allowed" (the same
// probes as `apps/agents/credentials/delegations.py`). GitHub's expiry header is not
// readable cross-origin, so the expiry date comes from the first machine that opens it.
import { jkt, sealValue, type Recipient } from '@/vault/zk'

export const GITHUB_API = 'https://api.github.com'
const PROBE_HEAD = 'canopy-permission-probe/does-not-exist'
const PROBE_SHA = '0'.repeat(39) + '1'
const TOKEN_SHAPE = /^[A-Za-z0-9_]+$/

export interface GitHubCheck { repo: string; ok: boolean; detail: string }
export interface GitHubProbe { login: string; user_id: number; name: string; checks: GitHubCheck[] }

/** What canopy-web says a token for this agent is sealed to (`GET …/github/recipients`). */
export interface GitHubRecipients {
  agent_id: string
  kv: number
  name: string
  repo?: string
  recipients: { kind: 'device' | 'member'; target: string; label?: string; public_jwk: { x?: string; y?: string } }[]
  refusal?: string | null
}

/** The body of `PUT …/github`: never the token. */
export interface SealedGitHubToken extends GitHubProbe { value_zk: string }

type Fetch = typeof fetch

function headers(token: string): Record<string, string> {
  return {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
  }
}

/** Who the token acts as. Throws a sentence a person can act on. */
export async function inspectToken(token: string, f: Fetch = fetch): Promise<Omit<GitHubProbe, 'checks'>> {
  if (!TOKEN_SHAPE.test(token)) {
    throw new Error(`That paste is not just a GitHub token (${token.length} characters, with spaces or punctuation) — `
      + 'copy only the token GitHub showed you, which starts with github_pat_ and is about 93 characters.')
  }
  let res: Response
  try {
    res = await f(`${GITHUB_API}/user`, { headers: headers(token) })
  } catch (e) {
    throw new Error(`Could not reach GitHub to check the token: ${e instanceof Error ? e.message : String(e)}`)
  }
  if (res.status === 401) throw new Error('GitHub rejected this token (401) — it is wrong, expired or revoked.')
  if (res.status !== 200) throw new Error(`GitHub answered ${res.status} for /user.`)
  const body = (await res.json()) as { login?: string; id?: number; name?: string | null }
  return { login: body.login ?? '', user_id: Number(body.id ?? 0), name: body.name || body.login || '' }
}

async function probe(f: Fetch, token: string, path: string, body: unknown): Promise<number | string> {
  try {
    const res = await f(`${GITHUB_API}${path}`, {
      method: 'POST', headers: { ...headers(token), 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    return res.status
  } catch (e) {
    return `could not reach GitHub: ${e instanceof Error ? e.message : String(e)}`
  }
}

/** Can this token SHIP to `repo` — push a branch, then open a pull request? Creates nothing. */
export async function checkRepo(token: string, repo: string, f: Fetch = fetch): Promise<GitHubCheck> {
  const pr = await probe(f, token, `/repos/${repo}/pulls`,
    { title: 'canopy permission probe', head: PROBE_HEAD, base: 'main' })
  if (typeof pr === 'string') return { repo, ok: false, detail: pr }
  if (pr === 403) return { repo, ok: false, detail: 'the token reaches this repo but lacks Pull requests: Read and write' }
  if (pr === 404) {
    return { repo, ok: false, detail: 'the token cannot see this repo — add it to the token\'s repository selection, '
      + 'and check its Resource owner is the repo\'s org' }
  }
  if (pr !== 422) return { repo, ok: false, detail: `GitHub answered ${pr}` }
  const push = await probe(f, token, `/repos/${repo}/git/refs`, { ref: `refs/heads/${PROBE_HEAD}`, sha: PROBE_SHA })
  if (typeof push === 'string') return { repo, ok: false, detail: push }
  if (push === 403 || push === 404) {
    return { repo, ok: false, detail: 'the token cannot push here — it needs Contents: Read and write, '
      + 'and this repo in its repository selection' }
  }
  if (push !== 422) return { repo, ok: false, detail: `GitHub answered ${push}` }
  return { repo, ok: true, detail: 'can push and open pull requests' }
}

/** Seal `token` to every recipient canopy-web named, each device's target recomputed
 *  from its key here: a thumbprint that does not match its key is refused, not trusted. */
export async function sealToken(token: string, to: GitHubRecipients): Promise<string> {
  if (to.refusal || !to.recipients.some((r) => r.kind === 'device')) {
    throw new Error(to.refusal || 'No machine that runs this agent can open a credential of yours yet.')
  }
  const recipients: Recipient[] = await Promise.all(to.recipients.map(async (r) => {
    if (r.kind === 'device') {
      const real = await jkt(r.public_jwk)
      if (real !== r.target) throw new Error(`canopy-web named ${r.label || 'a machine'} with a key that is not its own.`)
      return { kind: 'device' as const, target: real, publicJwk: r.public_jwk }
    }
    return { kind: 'member' as const, target: String(r.target), publicJwk: r.public_jwk }
  }))
  return sealValue(token, to.agent_id, to.name, to.kv, recipients)
}

/** Probe, then seal: what `PUT …/github` takes. Refuses (throws) before sealing anything
 *  when GitHub rejects the token or it cannot ship to the agent's repo. */
export async function prepareGitHubToken(raw: string, to: GitHubRecipients, f: Fetch = fetch): Promise<SealedGitHubToken> {
  const token = raw.trim()
  if (!token) throw new Error('Paste the token.')
  if (to.refusal) throw new Error(to.refusal)
  const who = await inspectToken(token, f)
  const checks = to.repo ? [await checkRepo(token, to.repo, f)] : []
  const failed = checks.find((c) => !c.ok)
  if (failed) throw new Error(`${failed.repo}: ${failed.detail}`)
  return { ...who, checks, value_zk: await sealToken(token, to) }
}
