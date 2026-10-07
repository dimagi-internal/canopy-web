import { test, expect, type Page } from '@playwright/test'

const acted = (page: Page) =>
  page.waitForResponse((r) => /\/tasks\/[^/]+\/actions$/.test(r.url()) && r.request().method() === 'POST')

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
  for (const section of ['projects', 'tasks', 'turns', 'schedules', 'skills']) {
    await expect(page.locator(`a[href$="/agents/echo/${section}"]`)).toBeVisible()
  }
})

test('an agent opens on Projects', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo')
  await expect(page).toHaveURL(/\/agents\/echo\/projects$/)
  await expect(page.getByTestId('project-row-P1')).toContainText('Connect stories')
})

test('a project page shows its tasks and the links the agent recorded', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/projects/P1')
  await expect(page.getByTestId('task-t3')).toBeVisible()
  await expect(page.getByTestId('task-t4')).toBeVisible()
  await expect(page.getByTestId('task-t1')).toHaveCount(0) // a one-off, not in P1
  await page.getByTestId('project-links').locator('summary').click()
  await expect(page.getByText('Demo story RUWOYD')).toBeVisible()
})

test('old addresses land on their successors', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/inbox')
  await expect(page).toHaveURL(/\/agents\/echo\/tasks\?waiting=me$/)
  await page.goto('/w/dimagi/agents/echo/work-products')
  await expect(page).toHaveURL(/\/agents\/echo\/projects$/)
  await page.goto('/w/dimagi/agents/echo/work')
  await expect(page).toHaveURL(/\/agents\/echo\/tasks$/)
})

test('Waiting on you shows open asks, actionable on the card', async ({ page }) => {
  await page.goto('/w/dimagi/agents/ada/tasks?waiting=me')
  await expect(page.getByTestId('filter-waiting')).toHaveAttribute('aria-pressed', 'true')
  const card = page.getByTestId('task-fa-hal-inbox')
  await expect(card).toContainText('hal: discard 81 junk/stale unread emails')
  await expect(card.getByRole('button', { name: 'Approve & run' })).toBeVisible()
  await expect(card.getByRole('button', { name: 'Decline' })).toBeVisible()
})

test('an agent with nothing waiting shows none of its other tasks there', async ({ page }) => {
  // echo's tasks are owned by people canopy does not know and ask nothing, so
  // none of them waits on the e2e user.
  await page.goto('/w/dimagi/agents/echo/tasks?waiting=me')
  await expect(page.getByTestId('filter-waiting')).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByTestId('task-t4')).toHaveCount(0)
})

test('status reports list the sync with its self-grades', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/syncs')
  await expect(page).toHaveURL(/\/agents\/echo\/turns#status-reports$/)
  await expect(page.getByText('Manager sync 1')).toBeVisible()
  await expect(page.getByText(/C\+/)).toBeVisible()
})

test('skills section lists the catalog', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/skills')
  await expect(page.getByText('email-communicator')).toBeVisible()
})

test('task board groups by who has the ball, with context + queue badge', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  for (const label of ['Suggested', 'Waiting on a human', 'Echo working']) {
    await expect(page.getByText(label, { exact: false }).first()).toBeVisible()
  }
  await expect(page.getByTestId('task-t1')).toHaveAttribute('data-status', 'suggested')
  await expect(page.getByTestId('task-t3')).toHaveAttribute('data-status', 'in_progress')
  await expect(page.getByText(/Strong near-miss/)).toBeVisible() // rationale on the card
  await expect(page.getByText(/queued for Echo/i)).toBeVisible() // the seeded pending action
})

test('Done shows finished tasks with their recorded result', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks?view=done')
  const card = page.getByTestId('task-t5') // done, with a seeded applied action
  await expect(card.getByTestId('task-last-activity')).toContainText('Shipped the agent workspace board.')
  await expect(card.getByTestId('task-last-activity')).toContainText(/Jun 17/)
})

test('the queue badge expands to the pending actions; activity stream lists history', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  // The badge starts as a count; clicking reveals which actions are pending.
  await page.getByRole('button', { name: /queued for Echo/i }).click()
  await expect(page.getByText(/dispatched/i).first()).toBeVisible()
  // The activity disclosure lists recent actions across the agent.
  await page.getByRole('button', { name: /^Activity/i }).click()
  await expect(page.getByTestId('agent-activity')).toContainText('completed')
})

test('a suggested card links its grounded source next to the rationale', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  const card = page.getByTestId('task-t1')
  const source = card.getByRole('link', { name: /source/i })
  await expect(source).toHaveAttribute('href', 'https://example.com/zegcawis')
})

test('reply leaves a comment for the agent', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  const card = page.getByTestId('task-t2')
  await card.getByTestId('task-reply-t2').fill('Start with the last three edits.')
  const [resp] = await Promise.all([acted(page), card.getByRole('button', { name: 'Reply' }).click()])
  expect(resp.status()).toBe(200)
  await expect(page.getByTestId('task-t2')).toHaveAttribute('data-status', 'suggested')
})

test('dispatch queues a "do it now" action for the agent', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  const card = page.getByTestId('task-t3') // waiting-on-a-human, in progress
  const [resp] = await Promise.all([acted(page), card.getByRole('button', { name: /do this now/i }).click()])
  expect(resp.status()).toBe(200)
})

test('mark done moves an in-progress task to Done', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/tasks')
  const card = page.getByTestId('task-t4') // Echo working
  const [resp] = await Promise.all([acted(page), card.getByRole('button', { name: /Mark done/i }).click()])
  expect(resp.status()).toBe(200)
  // Open shows live tasks only, so a finished one leaves it…
  await expect(page.getByTestId('task-t4')).toHaveCount(0)
  // …and is under Done.
  await page.getByTestId('filter-done').click()
  await expect(page.getByTestId('task-t4')).toHaveAttribute('data-status', 'done')
})
