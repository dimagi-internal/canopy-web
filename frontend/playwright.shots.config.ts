import base from './playwright.config'

/** Runs ONLY the multiplayer screenshot harness, which the default config
 *  ignores:  npx playwright test -c playwright.shots.config.ts */
export default {
  ...base,
  testIgnore: undefined,
  testMatch: /mp-shot\.spec\.ts/,
  timeout: 90_000,
  projects: [{ name: 'shots' }],
}
