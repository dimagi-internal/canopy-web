# E2E tests (Playwright)

Browser tests for the agent workspace (`/agents/echo`) — board grouping, the
section panels, and the five task actions (approve / decline / reply / dispatch / done),
including acting from the fleet's Waiting on you on `/supervisor`.

```bash
cd frontend && npm run test:e2e
```

Playwright boots two servers itself (`webServer` in `playwright.config.ts`):
- **API** on :8000 via `e2e/backend.sh` — a throwaway **SQLite** DB, `REQUIRE_AUTH=False`,
  migrated + seeded (`e2e/seed.py`), which also **mints a Django session** and writes the key
  to `e2e/.auth/session.txt`. No Google OAuth needed.
- **SPA** on :3000 via `npm run dev` (proxies `/api` → :8000).

`e2e/global-setup.ts` turns the minted session key into a Playwright `storageState`
cookie, so requests are authenticated exactly like a logged-in user (CSRF included).
