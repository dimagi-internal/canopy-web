# The browser extension's side panel

The Dimagi Canopy Chrome extension (its own repository) shows canopy **workspace-app pages** in
its side panel, chosen by the URL of the tab you are on; site **adapters** bundled in the
extension (Gmail first) let those pages read the page you are on. canopy-web's half is
here: which pages run where (the manifest), which of yours match a tab (one route), the
panel page the extension frames, and the gate every call to the tab passes. The extension
holds no credential: everything an app does to an outside system goes through canopy-web's
connections ([`vault-connections.md`](vault-connections.md) § Calls from screens).

## Where to start reading

| Piece | Where |
|---|---|
| A page's `browser` declaration, the adapter registry, Chrome's match-pattern rule | `apps/workspace_apps/browser.py` (docstring), validated from `apps/workspace_apps/manifest.py::validate` |
| Your apps for a tab: `GET /api/browser/apps` | `apps/workspace_apps/api_browser.py::list_browser_apps` |
| The allowed extensions, the framable panel shell | `settings.BROWSER_EXTENSION_IDS`, `apps/embedding/extension.py` |
| The panel: signed out, picker, drawing the page | `frontend/src/ext/ExtPanelPage.tsx`, `ExtSignedInPage.tsx` |
| Wire protocol v1 and the gate | `frontend/src/ext/extensionBridge.ts` (`ExtensionLink`, `BrowserGate`), `matchPattern.ts` |
| `canopy.browser` in the sandbox | `frontend/src/dynamicViews/frame/main.tsx::browserApi`, answered in `DynamicViewFrame.tsx` |

## Rules

- **A page reaches only what it declared.** A `browser` entry names a `member` page of the
  same app, an adapter, and capabilities and events from the registry
  (`browser.py::ADAPTERS`); its `matches` are https match patterns whose host is within the
  adapter's hosts. Unknown names fail validation. A template update that changes the
  declaration needs an admin (`template_diff.py`).
- **The canopy page enforces it, never the sandboxed code.** `BrowserGate` relays a request
  only for a declared capability, and only while the extension's last `context` says the tab
  in front matches the page's patterns; an event reaches the page only if declared, from its
  adapter, about that tab. `canopy.browser.open` needs a click on the page
  (`connectionCalls.ts::pageClickedFrame`) and an https URL; so does `canopy.open(url)` in the panel,
  which goes through the same gate. A `layout: "full"` page fills the panel below the picker, unpadded
  (`ExtPanelPage.tsx`); `canopy.page.setParams` there changes only `app.params`, as the panel has no
  address bar.
- **Origins on both hops.** The panel posts only to an allowed `chrome-extension://<id>` that is
  its parent (`location.ancestorOrigins`, else the first well-formed message's origin) and
  accepts a message only with `event.source === window.parent` from that origin. Every request
  has a fresh id and fails `timeout` after `REQUEST_TIMEOUT_MS`. The sandbox frame keeps
  accepting messages only from canopy's own origins.
- **Framing is paired, never open.** `/ext/panel/` is exempt from `X-Frame-Options` only with
  `frame-ancestors` listing exactly the allowed extension origins; with none configured it is a
  404. Every other page keeps `DENY`. The dynamic-view frame adds the same origins to its own
  `frame-ancestors` (checked against every ancestor) and nothing else: still `sandbox
  allow-scripts`, never `allow-same-origin`, `connect-src 'none'`. A bundle page's files carry the
  same `frame-ancestors` (`apps/workspace_apps/bundles.py::csp`), so it draws in the panel too, with
  `canopy.browser` from canopy's injected client. The service worker leaves
  `/ext/` to the network so these headers are never replaced by a cached shell.
- **The list shows only what the panel could draw.** `list_browser_apps` reads each organisation
  once (`wsvc.each_organisation`), plus a guest's own apps (`wsvc.guest_scopes`), and keeps an
  entry only when the caller holds the page's role (`access.roles_for`); a switched-off app is
  never offered. Each row carries the page's `matches` so the panel can check the tab again.

## Signing in, and the cookie assumption

The panel must render signed out (`apps/common/middleware.py::_PUBLIC_PAGES`): a framed Google
sign-in is refused. It shows **Sign in to Canopy**, which asks the extension (`canopy-ext:open`)
to open the login in a tab with `next` set to `/ext/signed-in/`; the panel checks the session
again on focus, on a change of tab and from its button.

The session cookie stays `SameSite=Lax` (`config/settings/production.py`). That works only
because Chrome treats an extension page that holds host permission for canopy as same-site
for the frames it embeds, so canopy's cookies go with the panel's requests. Nothing here
changes cookie settings to make it work. **To verify on a new Chrome:** load the unpacked dev
build, open the panel on a Gmail tab signed out, sign in from the button, click back into the
panel: it must show your apps without a reload. If it stays signed out, the cookie is not
being sent to the framed page, and the fix is a decision about cookies, not a code change here.

An agent testing the panel as itself (a live end-to-end run signed in to Google as the agent) is
refused at that sign-in unless its owner has switched on **Browser sign-in for testing** on the
agent's Settings → Security tab ([access.md](access.md) § Browser sign-in for testing).
