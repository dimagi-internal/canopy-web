// Browser sign-in for testing (apps/agents/browser_sign_in.py): whether an agent's own
// Google account may sign in to canopy-web at a browser, and for how long.
import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

export type BrowserSignInOut = components['schemas']['BrowserSignInOut']

export async function getBrowserSignIn(slug: string): Promise<BrowserSignInOut> {
  const res = await apiV2.GET('/api/agents/{slug}/browser-sign-in', { params: { path: { slug } } })
  return unwrap(res, 'Could not load browser sign-in') as BrowserSignInOut
}

/** Switching on needs a passkey step-up, which `apiV2` prompts for; off never does. */
export async function switchOnBrowserSignIn(slug: string, days: number, reason = ''): Promise<BrowserSignInOut> {
  const res = await apiV2.PUT('/api/agents/{slug}/browser-sign-in', {
    params: { path: { slug } },
    body: { days, reason },
  })
  return unwrap(res, 'Could not switch on browser sign-in') as BrowserSignInOut
}

export async function switchOffBrowserSignIn(slug: string): Promise<BrowserSignInOut> {
  const res = await apiV2.DELETE('/api/agents/{slug}/browser-sign-in', { params: { path: { slug } } })
  return unwrap(res, 'Could not switch off browser sign-in') as BrowserSignInOut
}
