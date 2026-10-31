# E2E tests (Playwright)

Browser tests for the agent workspace (`/agents/echo`) — board grouping, the
section panels, and the five task actions (approve / decline / reply / dispatch / done),
including acting from the fleet's Waiting on you on `/supervisor`.

```bash
cd frontend && npm run test:e2e
```

Playwright boots two servers itself (`webServer` in `playwright.config.ts`):
- **API** on :8000 via `e2e/backend.sh` — a throwaway **Postgres** (a `pgvector/pgvector:pg16`
  container when Docker is up; `E2E_DATABASE_URL` overrides; SQLite as the last resort), `REQUIRE_AUTH=False`,
  migrated + seeded (`e2e/seed.py`), which also **mints a Django session** and writes the key
  to `e2e/.auth/session.txt`. No Google OAuth needed.
- **SPA** on :3000 via `npm run dev` (proxies `/api` → :8000).

`e2e/global-setup.ts` turns the minted session key into a Playwright `storageState`
cookie, so requests are authenticated exactly like a logged-in user (CSRF included).

## Chat with a runner (`chat-runner.spec.ts`)

Chat sends queue real turns (`CHAT_STUB_EXECUTOR=False`), and **`e2e/fake_runner.py`**
answers them the way a production runner does: it heartbeats, claims, binds the
session, writes a real Claude Code `.jsonl` transcript and ships its rows with
`canopy_transcript` through the real runner client (`runner/canopy_runner`). Only the
agent's words are scripted, so the server's ingest, the socket, the reducer and the
rendering all run for real. `seed_runner.py` gives it a cloud runner the e2e user owns,
enrolled in `dimagi` and assigned Echo, plus a token in `.auth/`.

The message text picks what the agent does:

| Send | The agent |
|---|---|
| anything | one tool call, then `echo: <text>` |
| `e2e:tools N` | N tool calls, then a reply (a tool-heavy turn) |
| `e2e:slow N` | N tool calls a second apart (watch a turn live) |
| `e2e:hang` | one tool call, then waits until the turn is stopped |
| `e2e:fail` | one tool call, then the turn fails |
| `e2e:newfile` | continues in a NEW transcript file, as after a failover or `/clear` |

To add a scenario, script the agent's side in `FakeRunner.script` and assert what a
person sees. The runner's log is `$TMPDIR/e2e-fake-runner-<user>.log`.

```bash
cd frontend && npx playwright test e2e/chat-runner.spec.ts --project=desktop
E2E_API_PORT=8011 npx playwright test e2e/chat-runner.spec.ts   # when :8000 is taken
```

CI runs it in the merge queue as `Browser suite (chat)`, on Postgres.
