# canopy-client

Framework-free client for [canopy-web](https://github.com/dimagi-internal/canopy-web):
a delegated-token cache, the REST calls a chat needs, and the session WebSocket
URL. **Zero dependencies** — no React, no framework — so it works on a React 18
app, a Django template, or anything with `fetch` and `WebSocket`.

## Do you need this?

Probably not, if you just want a chat panel on your page. Use the drop-in
**widget** instead: one `<script>` tag served by canopy, nothing to install. See
the host guide, [`docs/architecture/embedding-a-canopy-agent.md`](https://github.com/dimagi-internal/canopy-web/blob/main/docs/architecture/embedding-a-canopy-agent.md).

Use `canopy-client` when you are building **your own** chat UI and need the
transport underneath it.

| package | what it is | depends on |
| --- | --- | --- |
| **canopy-client** (this) | transport: token cache, REST, socket URL, host bridge | nothing |
| **canopy-ui** | React 19 components: chat panel, `useSessionSocket`, design tokens | React 19 (peer) |
| the widget | `widget.js`, served by canopy at `/canopy/embed/widget.js` | nothing — not installed |

Neither npm package imports the other.

## Install

```bash
npm install canopy-client
```

Ships ES2020 JavaScript with TypeScript declarations, so it works with bundlers
that do not compile `node_modules` (e.g. webpack + babel with
`exclude: /node_modules/`).

## Use

```js
import { createCanopyClient } from 'canopy-client'

const canopy = createCanopyClient({
  baseUrl: 'https://labs.connect.dimagi.com/canopy',
  // YOUR backend endpoint that mints a short-lived canopy token for the
  // signed-in user. The app credential is a secret: it never reaches a browser,
  // which is why minting is a callback and not something this package does.
  fetchToken: async () => {
    const r = await fetch('/your-app/canopy/token', { method: 'POST', credentials: 'same-origin' })
    const { token, expires_at } = await r.json()
    return { token, expiresAt: expires_at }
  },
})

const agents = await canopy.rest.listAgents()
const sessions = await canopy.rest.listSessions()
const session = await canopy.rest.getSession(sessions[0].id)
await canopy.rest.send(session.id, 'what is stale here?', crypto.randomUUID())

// The socket URL with the current token already on it — or null before the
// first token is minted, so you never open a socket that will be refused.
const url = canopy.sessionSocketUrl(session.id)
```

The token is cached and re-minted shortly before it expires, and once more on a
401. Call `canopy.invalidateToken()` when your user signs out or switches
account.

Sessions are **created by your backend**, not by this package: the host stamps
provenance on a session server-side, and a browser that could set it could claim
another tenant's scope.

## Host bridge

`canopy-client/bridge` is the page side of "the agent can see and act on this
page": register a context provider and named actions, and the widget or your own
UI relays them.

```js
import { createHostBridge } from 'canopy-client/bridge'

const bridge = createHostBridge()
bridge.provideContext(() => ({ path: location.pathname }))
bridge.registerAction('scrollToRow', async ({ id }) => { /* … */ })
```

## Versions

`0.1.0` shipped TypeScript source and fails in a bundler that does not compile
`node_modules`. Use `0.2.0` or later.
