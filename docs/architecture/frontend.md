# Frontend: design system, themes, and the PWA shell

How the React SPA (`frontend/`) is styled and how it is served so a deploy is never hidden behind a stale cache. Deployment of the container itself is in [`deploy.md`](deploy.md).

## UI style

App UI is **dense and readable: tables, not cards.**

A core object's page (agent settings, workspace settings, a runner, a vault) is
built from `components/objectPage/` — `ObjectPage`, `ObjectTabs`, `Section` /
`Setting` — so they share one frame and one tab row, and improving the style is
one change (`pages.md` § One editor per object).

## API clients and errors

- Every `src/api/*` module calls the server through `apiV2` (`src/api/client.v2.ts`), so it gets the `/api/w/{ws}` rewrite, the 401 login bounce and the passkey step-up retry. Response types alias `components['schemas'][...]` from `generated.ts`; only free-form JSON fields (a review's `request_json`, a narrative's `narration`) are written out by hand. `chat.ts` uses raw `fetch` for routes that are not in the schema.
- A failed call throws the one `ApiError` (`src/api/problem.ts`). Its fields are documented on the class; use `unwrap(res, what)` for a call that returns data and `ensureOk(res, what)` for a 204, and branch on `e instanceof ApiError && e.status === …`. The per-area names (`MailApiError`, `VaultApiError`, …) are re-exports of the same class.
- Token-gated public viewers (`/w/<ws>/review|storyboard|narrative|ddd-release|share|walkthrough/…`) are scoped viewer paths. `apiV2` does not rewrite them and does not bounce their 401s to login, and `shareQuery(token, {ws})` passes the undeclared `?t=` token.

## Polling and live sockets

- **Poll with `usePolled(fn, {ms, deps})`** (`src/hooks/usePolled.ts`), never a hand-rolled `setInterval`. It drops results that land after unmount or a `deps` change, chains each tick on the settled fetch, and **stops while `document.visibilityState === 'hidden'`**, refetching at once on return — /supervisor is a phone PWA. `ms` may be a function of the last result (poll an open thread, stop once closed). Returns `{data, error, loading, refresh, setData}`.
- **A live WebSocket is `useReconnectingSocket(url, onFrame, opts)`** (`src/hooks/useReconnectingSocket.ts`): JSON frames, reconnect on a backoff ladder (`RECONNECT_BACKOFFS_MS`), `connectUrl` to resume from a cursor. The published `canopy-ui` chat and presence sockets keep their own loops (heartbeats, send receipts).

## Shared kit: `canopy-ui`

- The two-pane shell (left rail + scrolling main) used by the DDD and Agent workspaces, plus the design-system primitives, live in `frontend/packages/canopy-ui`, imported as `canopy-ui` with the subpath exports in its `package.json` `exports`. Surfaces consume it instead of re-implementing chrome.
- **Use its primitives instead of re-implementing them:** `StatusBadge tone=` (a status pill), `Segmented` (one-of-N, a radiogroup), `CopyButton` (copy with a "Copied" moment), `IconButton` (icon-only, `label` required) from `canopy-ui/ui`; `toneClasses`/`toneText`/`toneDot(tone)` (a status → class map is a `Record<status, StatusTone>`, not hand-written classes) and `copyText`/`useCopy` (never a raw `navigator.clipboard.writeText`) from `canopy-ui/lib`.
- It is published to public npm as the unscoped `canopy-ui`; naming, the version-bump CI check and release-on-merge: [embedding-internals.md § npm packages](embedding-internals.md#npm-packages).

## Design tokens (no raw palette literals)

The whole authenticated app styles off semantic tokens. `frontend/src/index.css` holds the per-theme values; the token names (`--color-*`) are mapped in `frontend/packages/canopy-ui/src/tokens/preset.css`. **Do not introduce raw Tailwind palette literals** (`stone-*`, `red-*`, `emerald-*`, …), and do not use `dark:` variants — components reference token names only, which is what makes both themes work.

- **Surfaces:** `bg-background` (page), `bg-card` (cards/popovers), `bg-muted` (fills/hover), `bg-input` (elevated controls).
- **Borders:** `border-border` (default), `border-input` (elevated/controls).
- **Brand:** `bg-primary` / `text-primary` / `border-primary`; `hover:bg-primary/90` for the button-hover darken.
- **Text emphasis ladder** (brightest → dimmest): `text-foreground` (headings) → `text-foreground-secondary` (body) → `text-muted-foreground` (meta/captions) → `text-foreground-subtle` (faint).
- **Status / categorical accents:** `success`, `warning`, `info`, `special`, `destructive`. Each has a `-foreground` for solid fills; tinted badges use `bg-<token>/10 text-<token> border-<token>/30`.
- **Placeholders:** `placeholder:text-placeholder` on an input surface (the `--placeholder` token: no one value clears 3:1 on both themes' `--input`, so it differs per theme in `index.css`).
- **The one exception:** the public share viewer `/w/:workspace/share/:token` (`frontend/src/pages/SessionSharePage.tsx` + `frontend/src/components/transcript/` + `frontend/src/components/ShareRouteErrorBoundary.tsx`) is a deliberately light-themed page mounted outside the app shell (`bg-white`) and uses neutral literals.
- **Enforced** by `frontend/src/designTokens.test.ts`, which scans `frontend/src/` and `frontend/packages/canopy-ui/src/` for palette literals and `dark:` variants. Its allowlists (`ALLOWED`, `PLAIN_ALLOWED`, each entry with a reason) are the share viewer above, the stock shadcn `dark:` variants and categorical per-person hues in `canopy-ui`, and media backdrops. A colour no token covers gets a token in `index.css`, not an allowlist entry. The same test bans literal `text-white` / `bg-white` / `bg-black` (a fill's text is `text-primary-foreground` or its own `-foreground`; a scrim is `bg-background/80`).

## Tables

Dense app tables are written with `<Th>`, `<Td>` and `<Tr>` from `frontend/src/components/DataTable.tsx`, not hand-copied `<th className="py-1 font-medium">` / `<td className="py-1.5 pr-3 …">` / `<tr className="border-b …">`. The props (density, tone, alignment, `hideBelow`, rule) and the density table (`PAD_CLASS`) are documented in that file.

- A row-dependent class (a status colour, `opacity-60`, a selected background) goes in `className` and is appended after the convention's classes. Semantic tokens only.
- `frontend/src/dataTableConvention.test.ts` fails on a raw `<th>`/`<td>`/`<tr>` whose `className` carries anything those props own, on any hand-written `<th>`, and on a raw `<td>`/`<tr>` with a computed `className` beyond the counts in `NOT_YET_MIGRATED` / `COMPUTED_CLASS`, which can only shrink. A genuinely different cell (its own padding, a one-off width) keeps its extra classes in `className` on a `Td`. A pattern that repeats across files gets a prop, not a copy. The `canopy-ui` `Table` primitives are a separate, card-bordered style and are not used here.

## Modals

An overlay on a scrim is the shared `Dialog` / `DialogContent` / `DialogTitle` from `canopy-ui/ui` (`frontend/packages/canopy-ui/src/ui/dialog.tsx`): it owns the focus trap, Escape, the scrim click, the built-in close button and `role="dialog"` with the title as its label. Size and surface go in `className` on `DialogContent` (`block w-[calc(100%-2rem)] max-w-lg bg-card p-4`; it is a grid with `p-6` by default). Base UI does not set `aria-modal`, and `pages/work/TriagePage.tsx` and `components/work/KeyboardHelp.tsx` look for `[aria-modal="true"]` to know another dialog owns the keyboard, so pass `aria-modal="true"` to `DialogContent`. A dialog that is mounted only while shown uses `<Dialog open onOpenChange={(o) => { if (!o) onClose() }}>`. A test that presses Escape fires it on the dialog (`fireEvent.keyDown(dialog, …)`), not on `window`.

A hand-built `fixed inset-0` overlay stays only where the shared one cannot do the job, each with a `shared-dialog-exception: <why>` comment (`grep -rn shared-dialog-exception frontend/src` lists them). `frontend/src/modalConvention.test.ts` fails on any other hand-built overlay and on a stale entry. An inline `role="dialog"` panel (the Emergency Kit, the recovery code, the vault unlock form, the memory popovers) is not an overlay and is not covered.

## Light and dark themes

- One token set, two themes, in `frontend/src/index.css`: `:root` is Warm Earth **light**, `.dark` is Warm Earth **dark** (the default). The light palette deepens brand/status hues (~600) for contrast on white.
- `frontend/index.html` applies `.dark` before first paint unless `localStorage.theme` is `light`, so there is no flash (keep it in step with `ThemeProvider.tsx`).
- `frontend/src/theme/ThemeProvider.tsx` (`useTheme`, `<ThemeToggle/>`) toggles and persists the class on `<html>`.

## PWA navigation: allowlist SPA routes, never denylist server routes

- The service worker handles a navigation **only** when its path matches a known SPA route prefix; every other navigation goes to the network and reaches Django. "Handles" means **network-first, with the precached `index.html` only as the offline fallback**.
- This fails safe: a new server route is excluded by construction; a forgotten SPA route only loses offline fallback (online it still resolves through `spa_view` in `config/views.py`). An iframe load is a navigation, so a server route the SW swallowed would render the SPA inside itself.
- The lists are `NAVIGATE_FALLBACK_ALLOWLIST` / `NAVIGATE_FALLBACK_DENYLIST` in `frontend/src/pwa/navigation-fallback.ts` (unit-tested in `navigation-fallback.test.ts`), imported by `frontend/vite.config.ts`.
- Server content under the allowlisted `/w/` prefix (`/w/<ws>/walkthrough/<id>/content`) is carved back out on the denylist (in workbox, denylist wins). The carve-out must end in `(?:\?.*)?$` because workbox matches `pathname + search` and embeds carry `?t=<share_token>`.
- The flat `/walkthrough/`, `/review/`, `/share/`, `/storyboard/`, `/narrative/`, `/ddd-release/` prefixes are not on the allowlist, so they reach Django's 404.

## The app shell is never served stale

`index.html` is the only file naming a build's content-hashed assets, so a stale copy renders a stale app. Four layers keep it fresh:

1. **Service worker.** In `frontend/vite.config.ts`, `navigateFallback` must be set to `undefined` **explicitly** (vite-plugin-pwa re-adds `'index.html'` when the key is absent) and `directoryIndex: null` (otherwise workbox maps a bare `/` onto the precached `index.html` ahead of any runtime route). A build-time guard in `vite.config.ts` reads the emitted `sw.js` and **fails the build** if a NavigationRoute is registered, `directoryIndex:null` is missing, or there is no NetworkFirst route — these are silent defaults a dependency bump can restore.
2. **HTTP caching.** `config/static_cache.py`: a content-hashed name gets `public, max-age=31536000, immutable`; everything else (including `spa_view`'s deep links) gets `no-cache`. The rule is total, so an unconsidered file costs a 304 rather than serving stale. An existing `immutable` from WhiteNoise on hashed `/static/` files is left alone.
3. **Tabs open across a deploy.** They ask for `lazy()` chunks that no longer exist; `lazyRoute` (`frontend/src/pwa/staleChunk.ts`) reloads once on a chunk-load failure, only when `navigator.onLine`, at most once per 10s — a reload loop is worse than the bug.
4. **A rolling deploy.** Old and new tasks serve side by side, so a page from one asks the other for a hashed name it never had. Each image carries every asset a live build used in the last 7 days (`frontend/scripts/carryAssets.mjs`, the Dockerfile's `frontend-dist` stage, fetched from the live site and listed in `/asset-history.json`; fails open to "carry nothing"). The other direction — a new page reaching a still-old task — is caught in the page: an inline guard in `frontend/index.html` reloads up to 3 times when an `/assets/` script fails before the app mounts, then says the app is updating instead of staying black.

Adopting a waiting SW (`frontend/src/pwa/registerPwa.ts`) is not urgent: a waiting SW only means a stale offline precache. A tab that stays visible is different: it keeps running the bundle it loaded until it reloads, and the SW only switches over once the page is hidden. So a waiting SW also raises `UpdateBanner` (`frontend/src/pwa/UpdateBanner.tsx`), which never reloads by itself. It is raised only when the build the server now serves is not the page's own (`frontend/src/pwa/thisBuild.ts`: the server's `sw.js` precaches the page's hashed entry script exactly when they are the same build) — a page loaded after a deploy is already current while the SW a long-open window installed still waits, and that SW is adopted quietly when the page is hidden. The poll in `registerPwa.ts` asks the same question itself, because `workbox-window` stops reporting updates after the first one it judges external.
