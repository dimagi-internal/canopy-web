# Local and server modes

An app page can reach Google two ways. **Local:** the tab beside the browser extension's panel,
through the extension's site adapter ([`browser-extension.md`](browser-extension.md)). Nothing leaves
the browser, and it acts on what the person is looking at. **Server:** the viewer's Google through
canopy-web ([`vault-connections.md`](vault-connections.md) § Calls from screens), which works without
the tab open. `canopy.google` puts both behind one set of named operations, and the person picks which
one runs.

## Where to start reading

| Piece | Where |
|---|---|
| The operations, their modes, both implementations, the answer shapes | `frontend/src/dynamicViews/googleOps.ts` (`GOOGLE_OPS`, `runGoogleOp`) |
| The person's mode per app, the panel's Local/Server switch | `frontend/src/ext/modeStore.ts`, `ExtPanelPage.tsx::ModeSwitch` |
| Where a call is answered (and the click read) | `DynamicViewFrame.tsx`, cases `google` and `mode-set` |
| `canopy.mode` / `canopy.google` in a page | `frame/main.tsx` (dynamic views), `apps/workspace_apps/bundle_client.js` (bundle pages) |
| What a page author is told | `apps/workspace_apps/guide.py` § LOCAL AND SERVER MODES |

## Rules

- **A mode is chosen, never fallen back to.** A page is in exactly one mode. An operation the mode
  lacks is refused `not_in_mode`; a local operation whose subject isn't on screen (no thread open,
  a different thread, no compose) is refused `not_on_screen`. Neither is retried in the other mode.
  A fallback would hide which way the person's mail went and would make a local failure look like
  success through the server. Every answer carries the `mode` it ran in.
- **The person chooses.** The mode is kept per app on this browser (`ModeStore`, `localStorage`
  `canopy.mode:<workspace>/<app>`), default `server`. It is set by the panel's switch or by a page's
  `canopy.mode.set(mode)`, which needs a click like any act. Every page of the app hears the
  change (`mode` message, `canopy.mode.on`). Outside the panel only `server` is available.
- **One shape in both modes.** An operation that has both modes answers the same keys either way
  (tested in `googleOps.test.ts`). Where the modes genuinely differ, the answer says so instead of
  pretending: `gmail.draft` is `where: "compose"` locally (open on screen, unsaved, for the person to
  send) and `where: "drafts"` on the server (saved in their Drafts). A `bodyHtml` draft is HTML (with a text alternative) only on the server, `format: "html"`; the adapter writes text, so locally it is the HTML's text, `format: "text"`. A local thread's collapsed
  messages may be snippets (`bodyComplete: false`).
- **No new gate, no new permission.** Local runs the adapter capability through `BrowserGate`, so the
  page must declare it in `browser` and the tab must match; an act needs a click. Server runs the
  Google call through `ConnectionCalls`, so the page must declare it in `connections`; a write needs
  a click's lease (`gmail.draft` opens it with `begin()` before reading the thread it replies to).
  The click is read when the message arrives, as for every other call. Nothing ever sends mail.
- **Adding an operation, or a mode to one**, is an entry in `GOOGLE_OPS` and its implementation in
  `LOCAL` / `SERVER`, plus a test that both modes answer the same shape. The adapter's own
  capabilities stay reachable through `canopy.browser` and the API through `canopy.connection`; this
  layer only chooses between them and shapes the answers.

## The operations

| Operation | Local | Server |
|---|---|---|
| `gmail.thread({threadId?})` | `read_open_thread`; a `threadId` must be the thread on screen | `GET gmail/threads/{id}?format=full` |
| `gmail.draft({kind, threadId?, to?, cc?, bcc?, subject?, bodyText \| bodyHtml})` | `start_compose` (act); a reply's `threadId` must be on screen | `GET gmail/threads/{id}?format=metadata` for a reply (recipients as Gmail's Reply / Reply all, `In-Reply-To`, `References`), then `POST gmail/drafts` |
| `gmail.open_email()` | `read_open_email` | none: the server can't see the screen |
| `gmail.fill_compose({bodyText, mode?, subject?})` | `fill_compose` (act) | none |
| `gmail.compose_recipients()` | `compose_recipients` | none |

Gmail search, Calendar, Sheets, Docs and Drive have no local form (Docs draws on a canvas; the rest
are about mail or files not on screen). A page calls them through `canopy.connection` as before.
