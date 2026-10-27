import { describe, expect, it, vi } from 'vitest'

import { checkRepo, inspectToken, prepareGitHubToken, sealToken, type GitHubRecipients } from './githubDelegation'
import { generateKeyPair, jkt, openSealedValue } from './zk'

const TOKEN = 'github_pat_olive'

/** Answers like GitHub for a fine-grained token: `pr`/`push` are the probes' statuses
 *  (422 allowed, 403 no permission, 404 unseen). Records every request it saw. */
function fakeGitHub({ user = 200, pr = 422, push = 422 }: { user?: number; pr?: number; push?: number } = {}) {
  const seen: { url: string; init?: RequestInit }[] = []
  const f = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const u = String(url)
    seen.push({ url: u, init })
    if (u.endsWith('/user')) {
      return new Response(JSON.stringify({ login: 'olive', id: 4242, name: 'Olive Owner' }), { status: user })
    }
    if (u.endsWith('/git/refs')) return new Response('{}', { status: push })
    return new Response('{}', { status: pr })
  })
  return { f: f as unknown as typeof fetch, seen }
}

async function recipients(): Promise<{ to: GitHubRecipients; device: Awaited<ReturnType<typeof generateKeyPair>>; target: string }> {
  const device = await generateKeyPair()
  const target = await jkt(device.publicJwk)
  return {
    device, target,
    to: { agent_id: 'agent-1', kv: 0, name: 'github-token', repo: 'dimagi-internal/echo', refusal: null,
          recipients: [{ kind: 'device', target, label: 'cloud-ec2-1', public_jwk: device.publicJwk }] },
  }
}

describe('delegating a GitHub token from the browser', () => {
  it('refuses a paste that is more than a token without asking GitHub', async () => {
    const { f } = fakeGitHub()
    await expect(inspectToken('github_pat_x and some ’ text', f)).rejects.toThrow(/not just a GitHub token/)
    expect(f).not.toHaveBeenCalled()
  })

  it('probes GitHub with the token, creating nothing', async () => {
    const { f, seen } = fakeGitHub()
    expect(await checkRepo(TOKEN, 'dimagi-internal/echo', f))
      .toEqual({ repo: 'dimagi-internal/echo', ok: true, detail: 'can push and open pull requests' })
    expect(seen.map((s) => s.url)).toEqual([
      'https://api.github.com/repos/dimagi-internal/echo/pulls',
      'https://api.github.com/repos/dimagi-internal/echo/git/refs',
    ])
    const bodies = seen.map((s) => JSON.parse(String(s.init?.body)))
    expect(bodies[0].head).toMatch(/^canopy-permission-probe\//)
    expect(bodies[1].sha).toBe('0'.repeat(39) + '1')
  })

  it('says what is missing when the token cannot ship to the repo', async () => {
    expect((await checkRepo(TOKEN, 'r/x', fakeGitHub({ pr: 403 }).f)).detail).toMatch(/Pull requests/)
    expect((await checkRepo(TOKEN, 'r/x', fakeGitHub({ pr: 404 }).f)).detail).toMatch(/Resource owner/)
    expect((await checkRepo(TOKEN, 'r/x', fakeGitHub({ push: 403 }).f)).detail).toMatch(/Contents/)
  })

  it('seals to the machines named, sends only ciphertext, and the machine opens it', async () => {
    const { to, device, target } = await recipients()
    const body = await prepareGitHubToken(` ${TOKEN} `, to, fakeGitHub().f)
    expect(body).toMatchObject({ login: 'olive', user_id: 4242, name: 'Olive Owner',
                                 checks: [{ repo: 'dimagi-internal/echo', ok: true }] })
    expect(JSON.stringify(body)).not.toContain(TOKEN)
    expect(body.value_zk.startsWith('zks1:')).toBe(true)
    expect(await openSealedValue(body.value_zk, 'agent-1', 'github-token', 'device', target, device.privateKey))
      .toBe(TOKEN)
  })

  it('seals nothing when GitHub rejects the token or a check fails', async () => {
    const { to } = await recipients()
    await expect(prepareGitHubToken(TOKEN, to, fakeGitHub({ user: 401 }).f)).rejects.toThrow(/rejected this token/)
    await expect(prepareGitHubToken(TOKEN, to, fakeGitHub({ pr: 404 }).f))
      .rejects.toThrow(/^dimagi-internal\/echo: the token cannot see this repo/)
  })

  it('refuses a machine whose thumbprint is not its key’s, and says why nothing can be sealed', async () => {
    const { to } = await recipients()
    const lying = { ...to, recipients: [{ ...to.recipients[0], target: 'not-its-thumbprint' }] }
    await expect(sealToken(TOKEN, lying)).rejects.toThrow(/not its own/)
    const nobody = { ...to, recipients: [], refusal: 'No machine that runs echo can open a credential of yours yet.' }
    await expect(prepareGitHubToken(TOKEN, nobody, fakeGitHub().f)).rejects.toThrow(/No machine that runs echo/)
  })
})
