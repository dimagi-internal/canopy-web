# Frontend: design system, themes, and the PWA shell

How the React SPA (`frontend/`) is styled and how it is served so a deploy is never hidden behind a stale cache. Deployment of the container itself is in [`deploy.md`](deploy.md).

## UI style

App UI is **dense and readable: tables, not cards.**

## API clients and errors

- Every `src/api/*` module calls the server through `apiV2` (`src/api/client.v2.ts`), so it gets the `/api/w/{ws}` rewrite, the 401 login bounce and the passkey step-up retry. Response types alias `components['schemas'][...]` from `generated.ts`; only free-form JSON fields (a review's `request_json`, a narrative's `narration`) are written out by hand. `chat.ts` still uses raw `fetch` because some of its routes (session secrets, MCP Apps View plumbing) are not in the schema.
- A failed call throws the one `ApiError` (`src/api/problem.ts`). It carries `status`, `code` (a top-level `error` member, or else the tail of the problem `type`), `title`, `detail`, `extras` and the raw `body`, and its `message` is the server's `detail` when there is one. Use `unwrap(res, what)` for a call that returns data and `ensureOk(res, what)` for a 204. Branch on `e instanceof ApiError && e.status === …`. The old names (`AgentApiError`, `WorkspaceApiError`, `ChatApiError`, …) are re-exports of the same class.
- Token-gated public viewers (`/w/<ws>/review|storyboard|narrative|ddd-release|share|walkthrough/…`) are scoped viewer paths. `apiV2` does not rewrite them and does not bounce their 401s to login, and `shareQuery(token, {ws})` passes the undeclared `?t=` token.

## Shared kit: `canopy-ui`

- The two-pane shell (left rail + scrolling main) used by the DDD and Agent workspaces, plus the design-system primitives, live in `frontend/packages/canopy-ui`, imported as `canopy-ui` with subpath exports `/ui`, `/lib`, `/chat`, `/presence`, `/shell`, `/tokens` (see its `package.json` `exports`). Surfaces consume it instead of re-implementing chrome.
- It is published to public npm as the unscoped `canopy-ui`; naming, the version-bump CI check and release-on-merge: [embedding-internals.md § npm packages](embedding-internals.md#npm-packages).
- Design: `docs/superpowers/specs/2026-06-17-shared-workbench-package-design.md`.

## Design tokens (no raw palette literals)

The whole authenticated app styles off semantic tokens. `frontend/src/index.css` defines the per-theme variables and `@theme inline` maps `--color-*` onto them. **Do not introduce raw Tailwind palette literals** (`stone-*`, `orange-*`, `zinc-*`, `slate-*`, `red-*`, `amber-*`, `emerald-*`, `sky-*`, `violet-*`), and do not use `dark:` variants — components reference token names only, which is what makes both themes work.

- **Surfaces:** `bg-background` (page), `bg-card` (cards/popovers), `bg-muted` (fills/hover), `bg-input` (elevated controls).
- **Borders:** `border-border` (default), `border-input` (elevated/controls).
- **Brand:** `bg-primary` / `text-primary` / `border-primary`; `hover:bg-primary/90` for the button-hover darken.
- **Text emphasis ladder** (brightest → dimmest): `text-foreground` (headings) → `text-foreground-secondary` (body) → `text-muted-foreground` (meta/captions) → `text-foreground-subtle` (faint).
- **Status / categorical accents:** `success` (opportunity), `warning` (ship-gap), `info` (alignment), `special` (pattern), `destructive` (errors). Each has a `-foreground` for solid fills; tinted badges use `bg-<token>/10 text-<token> border-<token>/30`.
- **Placeholders:** `placeholder:text-placeholder` on an input surface (the `--placeholder` token: no one value clears 3:1 on both themes' `--input`, so it differs per theme instead of a `dark:` variant).
- **The one exception:** the public share viewer `/w/:workspace/share/:token` (`frontend/src/pages/SessionSharePage.tsx` + `frontend/src/components/transcript/` + `frontend/src/components/ShareRouteErrorBoundary.tsx`) is a deliberately light-themed page mounted outside the app shell (`bg-white`) and uses neutral literals.
- **Enforced** by `frontend/src/designTokens.test.ts`, which scans `src/` and `packages/canopy-ui/src/` for palette literals and `dark:` variants. Its allowlist is the share viewer above plus, in the published `canopy-ui` package, the stock shadcn `ui/{badge,button,input,textarea}.tsx` `dark:` variants and the categorical per-person hues (`chat/personColor.ts`, `presence/avatar.ts`). A colour no token covers gets a token in `index.css`, not an allowlist entry.

## Light and dark themes

- One token set, two themes, in `frontend/src/index.css`: `:root` is Warm Earth **light**, `.dark` is Warm Earth **dark** (the default). The light palette deepens brand/status hues (~600) for contrast on white.
- `frontend/index.html` applies `.dark` before first paint unless `localStorage.theme` is `light`, so there is no flash.
- `frontend/src/theme/ThemeProvider.tsx` (`useTheme`, `<ThemeToggle/>`) toggles and persists the class on `<html>`; `AppLayout` drives it through `useTheme`, `PublicHeader` mounts `<ThemeToggle/>`.

## PWA navigation: allowlist SPA routes, never denylist server routes

- The service worker handles a navigation **only** when its path matches a known SPA route prefix; every other navigation goes to the network and reaches Django. "Handles" means **network-first, with the precached `index.html` only as the offline fallback**.
- This fails safe: a new server route is excluded by construction; a forgotten SPA route only loses offline fallback (online it still resolves through `spa_view` in `config/views.py`). An iframe load is a navigation, so a server route the SW swallowed would render the SPA inside itself.
- The lists are `NAVIGATE_FALLBACK_ALLOWLIST` / `NAVIGATE_FALLBACK_DENYLIST` in `frontend/src/pwa/navigation-fallback.ts` (unit-tested in `navigation-fallback.test.ts`), imported by `frontend/vite.config.ts`.
- Server content under the allowlisted `/w/` prefix (`/w/<ws>/walkthrough/<id>/content`) is carved back out on the denylist (in workbox, denylist wins). The carve-out must end in `(?:\?.*)?$` because workbox matches `pathname + search` and embeds carry `?t=<share_token>`.
- The retired flat `/walkthrough/`, `/review/`, `/share/`, `/storyboard/`, `/narrative/`, `/ddd-release/` prefixes are not on the allowlist, so they reach Django's 404.
- Design: `docs/superpowers/specs/2026-07-23-pwa-navigation-fallback-fail-safe-design.md`.

## The app shell is never served stale

`index.html` is the only file naming a build's content-hashed assets, so a stale copy renders a stale app. Three layers keep it fresh:

1. **Service worker.** In `frontend/vite.config.ts`, `navigateFallback` must be set to `undefined` **explicitly** (vite-plugin-pwa re-adds `'index.html'` when the key is absent) and `directoryIndex: null` (otherwise workbox maps a bare `/` onto the precached `index.html` ahead of any runtime route). A build-time guard in `vite.config.ts` reads the emitted `sw.js` and **fails the build** if a NavigationRoute is registered, `directoryIndex:null` is missing, or there is no NetworkFirst route — these are silent defaults a dependency bump can restore.
2. **HTTP caching.** `config/static_cache.py`: a content-hashed name gets `public, max-age=31536000, immutable`; everything else (including `spa_view`'s deep links) gets `no-cache`. The rule is total, so an unconsidered file costs a 304 rather than serving stale. An existing `immutable` from WhiteNoise on hashed `/static/` files is left alone.
3. **Tabs open across a deploy.** They ask for `lazy()` chunks that no longer exist; `lazyRoute` (`frontend/src/pwa/staleChunk.ts`) reloads once on a chunk-load failure, only when `navigator.onLine`, at most once per 10s — a reload loop is worse than the bug.

Adopting a waiting SW (`frontend/src/pwa/registerPwa.ts`) is not urgent: a waiting SW only means a stale offline precache. Inherent residual: a browser still holding the previous SW gets its shell from that worker one more time.
