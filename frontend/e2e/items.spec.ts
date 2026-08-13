import { test, expect } from '@playwright/test'

// The batch view: a fleet audit reviewed in one sitting. This is the surface that
// replaces the borrowed DDD review page — it belongs to the AGENT whose queue it
// is, not to a narrative. The old findings review had to borrow the DDD surface
// because this object did not exist, and conjured a phantom narrative doing it.

const BATCH = 'fleet-audit-2026-07-14'

test('a batch renders its items with their dispatch targets', async ({ page }) => {
  await page.goto(`/w/dimagi/agents/ada/items?batch=${BATCH}`)

  await expect(page.getByText('hal: discard 81 junk/stale unread emails')).toBeVisible()
  await expect(page.getByText('hal: ONE buried HUMAN email — Lily Olson')).toBeVisible()

  // Ada is the manager case: these dispatch to hal, not to herself.
  await expect(page.getByTestId('item-fa-hal-inbox')).toContainText('dispatches to hal')

  // No DDD chrome anywhere near it.
  await expect(page.getByText('DDD runs, grouped by narrative')).toHaveCount(0)
})

test('implementing an item decides it and dispatches its work', async ({ page }) => {
  await page.goto(`/w/dimagi/agents/ada/items?batch=${BATCH}`)

  const item = page.getByTestId('item-fa-lily')
  await item.getByRole('button', { name: 'implement' }).click()

  await expect(item.getByTestId('item-state')).toHaveText('decided')

  // Decided once: the buttons are gone, so it cannot be double-dispatched.
  await expect(item.getByRole('button', { name: 'implement' })).toHaveCount(0)
})

test('an open item shows in the agent inbox and the fleet supervisor', async ({ page }) => {
  // Phase 2: needs_you reads real Items alongside its remaining projections, so
  // Ada's audit reaches the supervisor's queue without borrowing anything.
  await page.goto('/w/dimagi/agents/ada/needs-you')
  await expect(page.getByText('hal: discard 81 junk/stale unread emails')).toBeVisible()

  await page.goto('/supervisor')
  // `item-inbox` (was `waiting-on-you`): the supervisor's queue became a plain
  // Item list when the needs_you aggregation was deleted, and the testid moved
  // with it. The spec kept asserting the old name and had been red ever since.
  await expect(page.getByTestId('item-inbox')).toContainText(
    'hal: discard 81 junk/stale unread emails',
  )
  // The inbox row names where implementing sends the work — Ada's fan-out,
  // visible without opening the item.
  await expect(page.getByTestId('item-inbox')).toContainText('dispatches to hal')
})

test('the unfiltered queue shows only what is still waiting; settled cards collapse', async ({
  page,
}) => {
  // The rot this guards against: Items are never deleted, only settled, so the
  // page grew to 32 cards — 30 of them decided or dismissed — and the 2 that
  // actually wanted an answer were buried in weeks of history.
  //
  // Asserted on the two items whose state is fixed regardless of what ran before:
  // fa-hal-inbox is never decided by another test, fa-old-settled is seeded
  // dismissed. Counting cards here would couple this to test order.
  await page.goto('/w/dimagi/agents/ada/items')

  await expect(page.getByText('hal: discard 81 junk/stale unread emails')).toBeVisible()
  await expect(page.getByText('waiting on you')).toBeVisible()

  // The settled card is present but tucked away, not competing for attention.
  const settled = page.getByTestId('items-settled')
  await expect(settled).toContainText('settled')
  await expect(
    page.getByText('hal: an old finding nobody needs to see again'),
  ).not.toBeVisible()

  await settled.getByText(/\d+ settled/).click()
  await expect(page.getByText('hal: an old finding nobody needs to see again')).toBeVisible()
})

test('a batch permalink still shows everything it decided', async ({ page }) => {
  // The deliberate exception: ?batch= names one sitting and is usually opened to
  // read back what was decided, so it must NOT hide settled cards.
  await page.goto('/w/dimagi/agents/ada/items?batch=fleet-audit-2026-06-30')

  await expect(page.getByText('hal: an old finding nobody needs to see again')).toBeVisible()
  await expect(page.getByTestId('items-settled')).toHaveCount(0)
})
