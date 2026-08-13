import { test, expect } from '@playwright/test'

// Reach a tab's content. Inbox is the default landing; Sessions/Agents need a click.
async function openTab(
  page: import('@playwright/test').Page,
  tab: 'inbox' | 'sessions' | 'agents' | 'runners',
) {
  if (tab !== 'inbox') await page.getByTestId(`tab-${tab}`).click()
}

test.describe('/supervisor', () => {
  test('renders without horizontal scroll on every tab', async ({ page }) => {
    await page.goto('/supervisor')
    await expect(page.getByTestId('supervisor-page')).toBeVisible()
    for (const tab of ['inbox', 'sessions', 'agents', 'runners'] as const) {
      await openTab(page, tab)
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      )
      expect(overflow, `tab ${tab} overflows`).toBeLessThanOrEqual(0)
    }
  })

  test('defaults to Inbox and deep-links via ?tab=', async ({ page }) => {
    // Default landing (what push drops you into) is Inbox: the waiting queue is
    // visible and the other tabs' content is not.
    await page.goto('/supervisor')
    // `item-inbox`/`inbox-empty` (were `waiting-on-you`/`waiting-empty`) — renamed when
    // the needs_you aggregation was deleted and the queue became a plain Item list.
    await expect(page.getByTestId('item-inbox').or(page.getByTestId('inbox-empty'))).toBeVisible()
    // The Sessions tab is now ChatSessionsPanel; the composer's `open-sessions` is gone.
    await expect(page.getByTestId('sessions-panel')).toBeHidden()

    // Deep-link straight to Agents.
    // Runners split out of the Agents tab into their own.
    await page.goto('/supervisor?tab=agents')
    await expect(page.locator('[data-testid^="agent-card-"]').first()).toBeVisible()
  })

  test('the inbox is above the fold', async ({ page }) => {
    await page.goto('/supervisor')
    const inbox = page.getByTestId('item-inbox').or(page.getByTestId('inbox-empty'))
    await expect(inbox).toBeInViewport()
  })

  test('one failed call does not blank the page', async ({ page }) => {
    // Abort the call the Inbox ACTUALLY makes. This used to abort
    // `/api/agents/needs-you`, deleted with the aggregation — so the route never
    // matched, nothing failed, and the test proved nothing while passing for it.
    await page.route('**/api/items/**', (r) => r.abort())
    await page.goto('/supervisor')
    await expect(page.getByTestId('supervisor-page')).toBeVisible()
    // Runners still render despite the Inbox fetch failing.
    await openTab(page, 'runners')
    await expect(page.getByTestId('runner-status').or(page.getByText('No runner paired'))).toBeVisible()
  })

  // ---------------------------------------------------------------------------
  // The three tests below drive the supervisor COMPOSER (`composer`,
  // `composer-agent`, `composer-skill`, `composer-mode-repo`, `composer-workspace`)
  // and the composer-era session list (`open-sessions`, `session-cloud-runner`).
  // None of those testids exist in the source any more: the Sessions tab was
  // rewritten around ChatSessionsPanel, whose creation entry point is "New chat
  // with <agent> or project".
  //
  // They are marked fixme rather than deleted because the BEHAVIOUR they pin is
  // still worth pinning — dispatching a launchable skill, a repo dispatch pinning
  // its workspace to the tenant endpoint, and continuing into an existing session.
  // Porting them needs the new panel's UX contract, which is not mine to invent.
  // Deleting them would quietly drop that coverage, which is how this file rotted
  // to 8/8 red without anyone noticing.
  // ---------------------------------------------------------------------------
  test.fixme('the composer dispatches a launchable command', async ({ page }) => {
    await page.goto('/supervisor')
    await openTab(page, 'sessions')
    const composer = page.getByTestId('composer')
    await expect(composer).toBeVisible()

    // Pick echo — the fleet has several agents and the default is whichever sorts
    // first; only echo carries a launchable skill in the seed.
    await page.getByTestId('composer-agent').selectOption('echo')

    // Only launchable skills appear — the seed's non-launchable email-communicator
    // must NOT be an option; story-ideation must.
    const skill = page.getByTestId('composer-skill')
    await expect(skill.locator('option', { hasText: 'story-ideation' })).toHaveCount(1)
    await expect(skill.locator('option', { hasText: 'email-communicator' })).toHaveCount(0)

    await skill.selectOption('story-ideation')
    // args_hint from the launchable skill drives the placeholder.
    await expect(page.getByTestId('composer-args')).toHaveAttribute('placeholder', 'topic (optional)')
    // The preview shows the exact command that will land in the session.
    await expect(page.getByTestId('composer-preview')).toHaveText('/echo:story-ideation')

    let posted: Record<string, unknown> | null = null
    await page.route('**/api/harness/turns/', async (route) => {
      posted = route.request().postDataJSON()
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ id: 't-1', agent_slug: 'echo', project: '', target: 'echo', status: 'queued' }),
      })
    })

    await page.getByTestId('composer-args').fill('bednets')
    await page.getByTestId('composer-send').click()

    await expect(page.getByTestId('composer-sent')).toBeVisible()
    expect(posted).toMatchObject({ agent_slug: 'echo', prompt: '/echo:story-ideation bednets' })
  })

  test.fixme('a repo dispatch pins its workspace and routes to the tenant endpoint', async ({ page }) => {
    await page.goto('/supervisor')
    await openTab(page, 'sessions')
    await page.getByTestId('composer-mode-repo').click()

    // A repo turn's tenant is first-class and defaults to dimagi (the e2e
    // workspace), shown in the selector — not hidden server magic.
    await expect(page.getByTestId('composer-workspace')).toHaveValue('dimagi')
    await page.getByTestId('composer-project').fill('canopy-web')

    let url: string | null = null
    let posted: Record<string, unknown> | null = null
    let headers: Record<string, string> = {}
    // The proof the workspace is pinned: the request lands on the TENANT-scoped
    // path /api/w/dimagi/…, not the flat mount (which would 422 a multi-workspace
    // user). WORKSPACE_HEADER drove the client-side rewrite.
    await page.route('**/api/w/*/harness/turns/', async (route) => {
      url = route.request().url()
      posted = route.request().postDataJSON()
      headers = route.request().headers()
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ id: 't-2', agent_slug: null, project: 'canopy-web', target: 'canopy-web', status: 'queued' }),
      })
    })

    await page.getByTestId('composer-args').fill('fix the header spacing')
    await page.getByTestId('composer-send').click()

    await expect(page.getByTestId('composer-sent')).toBeVisible()
    expect(url).toContain('/api/w/dimagi/harness/turns/')
    expect(posted).toMatchObject({ project: 'canopy-web', prompt: 'fix the header spacing' })
    // A stable per-(user,repo) thread_key so the NEXT dispatch continues this
    // session rather than forking a fresh emdash task — "drive the repo" is
    // iterative. The e2e user is e2e@dimagi.com.
    expect((posted as { origin_ref?: { thread_key?: string } }).origin_ref?.thread_key).toBe(
      'phone:e2e@dimagi.com:canopy-web',
    )
    // The pin header is consumed by the rewrite and must NOT reach the wire — it
    // is a client-side routing signal, not something the server should ever see.
    expect(headers['x-canopy-workspace']).toBeUndefined()
  })

  test.fixme('open sessions list and continue dispatches into that exact task', async ({ page }) => {
    await page.goto('/supervisor')
    await openTab(page, 'sessions')
    await expect(page.getByTestId('open-sessions')).toBeVisible()
    await expect(page.getByTestId('session-cloud-runner')).toBeVisible()

    let posted: Record<string, unknown> | null = null
    let url: string | null = null
    await page.route('**/api/w/*/harness/turns/', async (route) => {
      url = route.request().url()
      posted = route.request().postDataJSON()
      await route.fulfill({
        status: 201, contentType: 'application/json',
        body: JSON.stringify({ id: 't-9', agent_slug: null, project: 'canopy-web', target: 'canopy-web', status: 'queued' }),
      })
    })

    await page.getByTestId('session-input-cloud-runner').fill('rerun the failing test')
    await page.getByTestId('session-send-cloud-runner').click()

    await expect(page.getByTestId('session-sent-cloud-runner')).toBeVisible()
    expect(url).toContain('/api/w/dimagi/harness/turns/')  // tenant-pinned
    expect(posted).toMatchObject({ project: 'canopy-web', prompt: 'rerun the failing test' })
    expect((posted as { origin_ref?: { thread_key?: string } }).origin_ref?.thread_key).toBe('emdash:cloud-runner')
  })

  test('a runner shows not-ready and opens a detail view with the reason', async ({ page }) => {
    await page.goto('/supervisor?tab=runners')
    // the seeded runner is not-ready → the list shows the marker
    const notReady = page.locator('[data-testid^="runner-notready-"]').first()
    await expect(notReady).toBeVisible()
    // tap the runner row → detail view with the reason
    await page.getByTestId('runner-e2e-mbp').click()
    await expect(page.getByTestId('runner-detail-back')).toBeVisible()
    await expect(page.getByTestId('runner-detail-ready')).toHaveText('not ready')
    await expect(page.getByTestId('runner-detail-why')).toContainText('emdash CDP unreachable')
  })
})
