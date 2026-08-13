import { test, expect, type Page } from '@playwright/test'

const cmd = (page: Page) =>
  page.waitForResponse((r) => r.url().includes('/commands') && r.request().method() === 'POST')

test('agents list shows the Echo agent', async ({ page }) => {
  await page.goto('/w/dimagi/agents')
  await expect(page.getByText('Echo').first()).toBeVisible()
})

test('legacy /agents redirects to the active workspace', async ({ page }) => {
  await page.goto('/agents')
  await expect(page).toHaveURL(/\/w\/dimagi\/agents$/)
  await expect(page.getByText('Echo').first()).toBeVisible()
})

test('workspace rail exposes the sections', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo')
  await expect(page.locator('a[href$="/agents/echo/tasks"]')).toBeVisible()
  await expect(page.locator('a[href$="/agents/echo/syncs"]')).toBeVisible()
  await expect(page.locator('a[href$="/agents/echo/skills"]')).toBeVisible()
})

test('the inbox is the default landing, banded by kind, with actionable items', async ({
  page,
}) => {
  // Rewritten for the post-#304 inbox. It used to assert echo's TASK-derived bands
  // (`needsyou-band-*` at /needs-you, with Accept/Decline on suggested tasks). That
  // aggregation was deleted deliberately: the inbox is a pure Item query now, the
  // route is /inbox, and the testids are `inbox-band-*`. So the assertions move to
  // ada, who has Items — echo's five tasks correctly no longer surface here.
  await page.goto('/w/dimagi/agents/ada')
  await expect(page).toHaveURL(/\/agents\/ada\/inbox$/)
  await expect(page.getByRole('heading', { name: 'Inbox' }).first()).toBeVisible()

  // review band: a decision awaiting implement/skip/defer
  await expect(page.getByTestId('inbox-band-review')).toContainText(
    'hal: discard 81 junk/stale unread emails',
  )
  // The FYI 'notify' band stays gone (#273) — the inbox is action-only.
  await expect(page.getByTestId('inbox-band-notify')).toHaveCount(0)
  await expect(page.getByText(/waiting on you/)).toBeVisible()
})

test('an agent with nothing open says so instead of showing its task list', async ({ page }) => {
  // The other half of the #304 change, and the reason the old test could not simply
  // be renamed: echo has five seeded tasks and zero Items, and the inbox is now
  // Items-only — so the correct rendering is the empty state, not those tasks.
  await page.goto('/w/dimagi/agents/echo/inbox')
  await expect(page.getByText(/Nothing needs you right now/)).toBeVisible()
  await expect(page.getByText('Ideas backlog upkeep')).toHaveCount(0)
})

test('inbox cards are the actionable board card, not a bounce link', async ({ page }) => {
  await page.goto('/w/dimagi/agents/ada/inbox')
  const review = page.getByTestId('inbox-band-review')
  // The real card — you decide here, in place. Matched on testid, not button text:
  // the labels render lowercase and are capitalised in CSS, so an accessible-name
  // match on /^Implement$/ silently finds nothing.
  await expect(review.locator('[data-testid^="item-implement-"]').first()).toBeVisible()
  await expect(review.locator('[data-testid^="item-skip-"]').first()).toBeVisible()
  await expect(review.locator('[data-testid^="item-defer-"]').first()).toBeVisible()
  // ...and NOT a bare link that dumps you to the generic board.
  await expect(review.locator('a[href$="/tasks"]')).toHaveCount(0)
})

test('the rail exposes the inbox', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  await expect(page.locator('a[href$="/agents/echo/inbox"]')).toBeVisible()
})

test('overview shows the latest sync', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/overview')
  await expect(page.getByText('Manager sync 1')).toBeVisible()
})

test('syncs section lists the sync with its self-grades', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/syncs')
  await expect(page.getByText('Manager sync 1')).toBeVisible()
  await expect(page.getByText(/C\+/)).toBeVisible()
})

test('work products section lists the deliverable', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/work-products')
  await expect(page.getByText('Demo story RUWOYD')).toBeVisible()
})

test('skills section lists the catalog', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/skills')
  await expect(page.getByText('email-communicator')).toBeVisible()
})

test('task board groups by who has the ball, with context + queue badge', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  for (const label of ['Suggested', 'Waiting on a human', 'Echo working', 'Done']) {
    await expect(page.getByText(label, { exact: false }).first()).toBeVisible()
  }
  await expect(page.getByTestId('task-t1')).toHaveAttribute('data-status', 'suggested')
  await expect(page.getByTestId('task-t3')).toHaveAttribute('data-status', 'in_progress')
  await expect(page.getByText(/Strong near-miss/)).toBeVisible() // rationale on the card
  await expect(page.getByText(/queued for Echo/i)).toBeVisible() // the seeded pending command
})

test('an applied command surfaces its result + timestamp on the task card', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  const card = page.getByTestId('task-t5') // done, with a seeded applied command
  await expect(card.getByTestId('task-last-activity')).toContainText('Shipped the agent workspace board.')
  await expect(card.getByTestId('task-last-activity')).toContainText(/Jun 17/)
})

test('the queue badge expands to the pending commands; activity stream lists history', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  // The badge starts as a count; clicking reveals which commands are pending.
  await page.getByRole('button', { name: /queued for Echo/i }).click()
  await expect(page.getByText(/dispatched/i).first()).toBeVisible()
  // The activity disclosure lists recent commands across the agent.
  await page.getByRole('button', { name: /^Activity/i }).click()
  await expect(page.getByTestId('agent-activity')).toContainText('completed')
})

test('a suggested card links its grounded source next to the rationale', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  const card = page.getByTestId('task-t1')
  const source = card.getByRole('link', { name: /source/i })
  await expect(source).toHaveAttribute('href', 'https://example.com/zegcawis')
})

test('accept flips a suggested task to in progress and clears its Accept button', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  const card = page.getByTestId('task-t2')
  await expect(card).toHaveAttribute('data-status', 'suggested')
  const [resp] = await Promise.all([cmd(page), card.getByRole('button', { name: /^Accept$/ }).click()])
  expect(resp.status()).toBe(201)
  await expect(page.getByTestId('task-t2')).toHaveAttribute('data-status', 'in_progress')
  await expect(page.getByTestId('task-t2').getByRole('button', { name: /^Accept$/ })).toHaveCount(0)
})

// #209 made Decline one click (the reason only ever fed the agent as context);
// "＋ reason" is now the opt-in path. That change left this spec asserting a
// reason prompt that no longer appears — Playwright doesn't run in CI, so it
// went unnoticed. Covers the one-click path only: "＋ reason" needs its own
// suggested task, and these specs share one DB in file order (t1/t2 are spent
// by here, and Decline renders only while status=suggested).
test('decline is one click — no reason required', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  const card = page.getByTestId('task-t1')
  const [resp] = await Promise.all([cmd(page), card.getByRole('button', { name: /^Decline$/ }).click()])
  expect(resp.status()).toBe(201)
  await expect(page.getByTestId('task-t1')).toHaveAttribute('data-status', 'declined')
})

test('dispatch queues a "do it now" command for the agent', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  const card = page.getByTestId('task-t3') // waiting-on-a-human, in progress
  const [resp] = await Promise.all([cmd(page), card.getByRole('button', { name: /do this now/i }).click()])
  expect(resp.status()).toBe(201)
})

test('mark done moves an in-progress task to Done', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  const card = page.getByTestId('task-t4') // Echo working
  const [resp] = await Promise.all([cmd(page), card.getByRole('button', { name: /Mark done/i }).click()])
  expect(resp.status()).toBe(201)
  await expect(page.getByTestId('task-t4')).toHaveAttribute('data-status', 'done')
})
