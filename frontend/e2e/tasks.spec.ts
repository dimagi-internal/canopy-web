import { test, expect, type Page } from '@playwright/test'

// Asks on tasks: Ada's fleet audit, reviewed in one sitting, and the same rows
// in the fleet's Waiting on you. An ask is a property of a task — there is one
// card (TaskCard) and five actions, wherever the task appears.

const BATCH = 'fleet-audit-2026-07-14'

const acted = (page: Page) =>
  page.waitForResponse((r) => /\/tasks\/[^/]+\/actions$/.test(r.url()) && r.request().method() === 'POST')

test('a batch renders its asks with where approving sends the work', async ({ page }) => {
  await page.goto(`/w/dimagi/agents/ada/tasks?batch=${BATCH}`)

  await expect(page.getByText('hal: discard 81 junk/stale unread emails')).toBeVisible()
  await expect(page.getByText('hal: ONE buried HUMAN email — Lily Olson')).toBeVisible()

  // Ada is the manager case: approving runs the work on hal, not on herself.
  await expect(page.getByTestId('task-fa-hal-inbox')).toContainText('runs on hal')

  // No DDD chrome anywhere near it.
  await expect(page.getByText('DDD runs, grouped by narrative')).toHaveCount(0)
})

test('an old /items batch link still opens its sitting', async ({ page }) => {
  await page.goto(`/w/dimagi/agents/ada/items?batch=${BATCH}`)
  await expect(page).toHaveURL(new RegExp(`/agents/ada/tasks\\?batch=${BATCH}$`))
  await expect(page.getByText('hal: ONE buried HUMAN email — Lily Olson')).toBeVisible()
})

test('a batch permalink still shows what it declined', async ({ page }) => {
  // ?batch= names one sitting and is usually opened to read back what was
  // decided, so it is not narrowed to open tasks.
  await page.goto('/w/dimagi/agents/ada/tasks?batch=fleet-audit-2026-06-30')
  await expect(page.getByTestId('task-fa-old-settled')).toHaveAttribute('data-status', 'declined')
})

test('approving from Waiting on you runs the task and takes it off the queue', async ({ page }) => {
  await page.goto('/supervisor?tab=waiting')
  const queue = page.getByTestId('waiting-on-you')
  const card = queue.getByTestId('task-fa-lily')
  // Tagged with its agent: the fleet queue spans agents.
  await expect(card).toContainText('ada')
  await expect(card).toContainText('runs on hal')

  const [resp] = await Promise.all([acted(page), card.getByRole('button', { name: 'Approve & run' }).click()])
  expect(resp.status()).toBe(200)

  // Approved: the ask is closed, so it no longer waits on anyone.
  await expect(queue.getByTestId('task-fa-lily')).toHaveCount(0)
  await page.goto(`/w/dimagi/agents/ada/tasks?batch=${BATCH}`)
  await expect(page.getByTestId('task-fa-lily')).toHaveAttribute('data-status', 'in_progress')
})

test('answering a question closes the ask', async ({ page }) => {
  await page.goto('/w/dimagi/agents/ada/tasks?waiting=me')
  const card = page.getByTestId('task-fa-question')
  await expect(card.getByTestId('ask-fa-question')).toContainText('Archiving is reversible')

  await card.getByTestId('task-reply-fa-question').fill('Archive them.')
  const [resp] = await Promise.all([acted(page), card.getByRole('button', { name: 'Answer & run' }).click()])
  expect(resp.status()).toBe(200)

  await expect(page.getByTestId('task-fa-question')).toHaveCount(0)
})

test('declining a review closes it without running anything', async ({ page }) => {
  await page.goto('/w/dimagi/agents/ada/tasks?waiting=me')
  const card = page.getByTestId('task-fa-hal-inbox')
  const [resp] = await Promise.all([acted(page), card.getByRole('button', { name: 'Decline' }).click()])
  expect(resp.status()).toBe(200)
  await expect(page.getByTestId('task-fa-hal-inbox')).toHaveCount(0)
})
