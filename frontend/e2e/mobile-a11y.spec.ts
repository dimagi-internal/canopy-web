import { test, expect } from '@playwright/test'

// Guards for two defects found by driving the DEPLOYED app as an installed PWA on a
// Pixel 7 — neither of which any existing check could see, because both are about
// COMPUTED style rather than markup.
//
// Runs on the mobile project (see playwright.config testMatch).

const SURFACES = ['/supervisor', '/w/dimagi/agents/ada/items', '/w/dimagi/agents/ada/inbox']

test.describe('mobile', () => {
  // The gate is 2.0, and that is deliberately BELOW the 3:1 this should reach.
  //
  // What was shipped: four inputs used `bg-input placeholder:text-foreground-subtle`,
  // and those two tokens are the SAME oklch value in dark mode — contrast 1.00, the
  // hint literally unreadable. A fifth set no placeholder colour at all and inherited
  // the browser default: 1.08. Those are the bug this catches, and they are fixed.
  //
  // Moving to `--muted-foreground` gets dark mode to 2.15 (light mode is already 3.21,
  // same token on a light `--input`). Clearing 3:1 in dark needs a placeholder colour
  // around oklch(0.64) — which means a new per-theme token, because no single value
  // clears 3:1 against BOTH a dark `--input` (0.374) and a light one (0.869). That is a
  // palette decision, not a test decision, so it is raised rather than smuggled in here.
  // Raise this constant to 3 when that token lands.
  const MIN_CONTRAST = 2.0

  test('no invisible placeholders', async ({ page }) => {
    // `--foreground-subtle` and `--input` are the SAME oklch value in dark mode, so
    // `bg-input placeholder:text-foreground-subtle` rendered the answer box on a
    // question card as a dead grey blob: contrast 1.00, the hint literally unreadable.
    // Four inputs shipped that combination. This fails if any comes back.
    for (const path of SURFACES) {
      await page.goto(path)
      await page.waitForTimeout(1500)
      const bad = await page.evaluate((MIN_CONTRAST) => {
        // Resolve ANY CSS colour (this theme is authored in oklch, which
        // getComputedStyle returns verbatim) to sRGB by letting the canvas
        // rasterise it. Parsing the string by hand silently mis-reads oklch
        // components as RGB — which is exactly how the first version of this
        // test produced meaningless ratios.
        const cvs = document.createElement('canvas')
        cvs.width = cvs.height = 1
        const ctx = cvs.getContext('2d')!
        const rgb = (c: string) => {
          ctx.clearRect(0, 0, 1, 1)
          ctx.fillStyle = '#000'
          ctx.fillStyle = c
          ctx.fillRect(0, 0, 1, 1)
          const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data
          return [r, g, b]
        }
        const lum = (c: string) => {
          const [r, g, b] = rgb(c).map((v) => {
            const x = v / 255
            return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4
          })
          return 0.2126 * r + 0.7152 * g + 0.0722 * b
        }
        const out: string[] = []
        document.querySelectorAll('input[placeholder], textarea[placeholder]').forEach((el) => {
          const ph = getComputedStyle(el, '::placeholder').color
          let n: Element | null = el
          let bg = getComputedStyle(document.body).backgroundColor
          while (n) {
            const c = getComputedStyle(n).backgroundColor
            if (c && !/rgba\(0, 0, 0, 0\)/.test(c) && c !== 'transparent') { bg = c; break }
            n = n.parentElement
          }
          const a = lum(ph), b = lum(bg)
          const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
          if (ratio < MIN_CONTRAST) {
            out.push(`"${el.getAttribute('placeholder')}" ${ph} on ${bg} = ${ratio.toFixed(2)}`)
          }
        })
        return out
      }, MIN_CONTRAST)
      expect(bad, `unreadable placeholder(s) on ${path}`).toEqual([])
    }
  })

  test('decision controls are thumb-sized', async ({ page }) => {
    // Measured 25-27px tall on the deployed app — the implement/skip/defer/answer row
    // is the surface the product exists for and was the least tappable thing on it.
    // 44px is the floor Apple and Google both publish.
    await page.goto('/w/dimagi/agents/ada/items')
    await page.waitForTimeout(1500)
    const small = await page.evaluate(() => {
      const out: string[] = []
      document
        .querySelectorAll('[data-testid^="item-implement-"], [data-testid^="item-skip-"], [data-testid^="item-defer-"], [data-testid^="item-answer-"]')
        .forEach((el) => {
          const r = el.getBoundingClientRect()
          if (r.height > 0 && r.height < 44) {
            out.push(`${el.getAttribute('data-testid')} ${Math.round(r.width)}x${Math.round(r.height)}`)
          }
        })
      return out
    })
    expect(small, 'decision controls under 44px on a phone').toEqual([])
  })
})
