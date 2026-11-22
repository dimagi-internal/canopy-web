/*
 * canopy's client for a workspace app's BUNDLE page (apps/workspace_apps/bundles.py), injected
 * into every HTML file of the bundle ahead of the page's own scripts. A classic script, so
 * `window.canopy` exists before any of the page's code runs.
 *
 * The same calls a JSX page has (frontend/src/dynamicViews/frame/main.tsx), over the same
 * postMessage protocol (frontend/src/dynamicViews/protocol.ts): the document is an opaque
 * origin with no network but its own files, so everything goes to the canopy page that framed
 * it, which answers by the app's rules, as the viewer. Messages are accepted only from
 * window.parent at an origin the server named (`window.CANOPY_DV.origins`).
 *
 * What a bundle page adds: `canopy.ready` (a promise of the context, since the page's own
 * scripts run before canopy has spoken), `canopy.app` once it has, `canopy.open(page, params,
 * {newTab})` to open another page of the app (by slug, or by a bundle path such as
 * "kanban/kanban.html?id=5"), and links: a click on a link to another file of the bundle opens
 * that page, any other link leaves through `canopy.navigate`.
 */
(function () {
  'use strict'
  var CHANNEL = 'canopy-dynamic-view'
  var ORIGINS = (window.CANOPY_DV && window.CANOPY_DV.origins) || []
  var ancestor = location.ancestorOrigins && location.ancestorOrigins[0]
  var parentOrigin = ancestor && ORIGINS.indexOf(ancestor) >= 0 ? ancestor : null
  // This file's directory in the bundle: /…/app-bundles/<id>/ (the bundle's root).
  var ROOT = (function () {
    var m = location.pathname.match(/^(.*\/app-bundles\/[0-9a-f]{64}\/)/)
    return m ? m[1] : '/'
  })()

  function toHost(msg) {
    msg.channel = CHANNEL
    var targets = parentOrigin ? [parentOrigin] : ORIGINS
    for (var i = 0; i < targets.length; i++) window.parent.postMessage(msg, targets[i])
  }

  var nextId = 1
  var pending = {}
  var statusListeners = {}
  var pageActions = {}
  var browserHandlers = {}
  var context = null
  var resolveReady
  var ready = new Promise(function (resolve) { resolveReady = resolve })

  function ask(build) {
    var id = nextId++
    return new Promise(function (resolve, reject) {
      pending[id] = { resolve: resolve, reject: reject }
      toHost(build(id))
    })
  }

  function rejection(msg) {
    var e = new Error(msg.error)
    if (msg.code) { e.error = msg.code; e.code = msg.code }
    if (msg.connect_url) e.connect_url = msg.connect_url
    return e
  }

  function strings(params) {
    var out = {}
    Object.keys(params || {}).forEach(function (k) {
      if (params[k] != null) out[k] = String(params[k])
    })
    return out
  }

  /** A slug, or a bundle path ("kanban/kanban.html?id=5", "/kanban/kanban.html"), to {page, params}. */
  function resolvePage(target, params) {
    var pages = (context && context.pages) || []
    var given = String(target || '')
    for (var i = 0; i < pages.length; i++) if (pages[i].slug === given) return { page: given, params: strings(params) }
    var u
    try { u = new URL(given, location.origin + ROOT) } catch (e) { return null }
    if (u.pathname.indexOf(ROOT) !== 0) return null
    var path = decodeURIComponent(u.pathname.slice(ROOT.length))
    for (var j = 0; j < pages.length; j++) {
      if (pages[j].entry === path) {
        var p = {}
        u.searchParams.forEach(function (v, k) { p[k] = v })
        return { page: pages[j].slug, params: Object.assign(p, strings(params)) }
      }
    }
    return null
  }

  var canopy = {
    /** Resolves {app, scope, browser} once the canopy page has drawn this page. */
    ready: ready,
    /** The app's calls (see appApi), once ready; null before. */
    app: null,
    /** The tab beside the browser extension's panel; null outside the panel. */
    browser: null,
    scope: null,
    read: function (model, opts) {
      opts = opts || {}
      return ask(function (id) { return { type: 'read', id: id, request: { model: model, params: opts.params, path: opts.path } } })
    },
    navigate: function (path) {
      if (typeof path === 'string' && path) toHost({ type: 'navigate', path: path })
    },
    /** Open another page of this app as a full canopy page. `{newTab: true}` opens a tab (from a click). */
    open: function (target, params, options) {
      var to = resolvePage(target, params)
      if (!to) throw new Error('this app has no page "' + target + '"')
      toHost({ type: 'go', page: to.page, params: to.params, newTab: !!(options && options.newTab) })
    },
    runAgentTask: function (request) {
      request = request || {}
      var onStatus = request.onStatus
      var rest = Object.assign({}, request)
      delete rest.onStatus
      var asked = 0
      return ask(function (id) {
        asked = id
        if (typeof onStatus === 'function') statusListeners[id] = onStatus
        return { type: 'agent-task', id: id, request: rest }
      }).finally(function () { delete statusListeners[asked] })
    },
    agentTask: function (taskId) {
      return ask(function (id) { return { type: 'agent-task-read', id: id, taskId: taskId } })
    },
    connection: {
      request: function (req) { return ask(function (id) { return { type: 'connection-call', id: id, request: req } }) },
      begin: function () { return ask(function (id) { return { type: 'connection-begin', id: id } }) },
    },
    page: {
      setState: function (state) { toHost({ type: 'page-state', state: state }) },
      registerAction: function (name, run, options) {
        if (typeof run !== 'function') throw new Error('registerAction(name, run, options): run must be a function')
        pageActions[name] = run
        toHost({ type: 'page-action-register', spec: Object.assign({ name: name }, options || {}) })
      },
      unregisterAction: function (name) {
        delete pageActions[name]
        toHost({ type: 'page-action-unregister', name: name })
      },
      ask: function (text) {
        if (typeof text === 'string' && text.trim()) toHost({ type: 'page-ask', text: text })
      },
    },
  }

  function appApi(ctx) {
    function call(request) { return ask(function (id) { return { type: 'app', id: id, request: request } }) }
    var model = ctx.dataModel || {}
    return {
      key: ctx.key, name: ctx.name, page: ctx.page, params: ctx.params || {}, roles: ctx.roles || [],
      dataModel: model, settings: ctx.settings || {}, draft: !!ctx.draft,
      query: function (collection, opts) { return call(Object.assign({ op: 'query', collection: collection }, opts || {})) },
      act: function (action, opts) { return call(Object.assign({ op: 'act', action: action }, opts || {})) },
      bulk: function (action, opts) { return call(Object.assign({ op: 'bulk', action: action }, opts || {})) },
      link: function (link, record) { return call({ op: 'link', link: link, record: record }) },
      history: function (record) { return call({ op: 'history', record: record }) },
      upload: function (collection, field, file) {
        return file.arrayBuffer().then(function (bytes) {
          return call({ op: 'upload', collection: collection, field: field, name: file.name, type: file.type, bytes: bytes })
        })
      },
      openFile: function (id) { call({ op: 'open_file', id: id }) },
      go: function (page, params) { canopy.open(page, params) },
      field: function (collection, name) { return ((model[collection] || {}).fields || {})[name] },
      tasks: {
        declared: model._tasks || null,
        query: function (filters) { return call({ op: 'tasks_query', filters: filters || {} }) },
        act: function (name, opts) { return call(Object.assign({ op: 'tasks_act', name: name }, opts || {})) },
      },
    }
  }

  function browserApi(info) {
    function call(request) {
      return ask(function (id) { return { type: 'browser', id: id, request: request } })
    }
    return {
      adapter: info.adapter,
      capabilities: (info.capabilities || []).slice(),
      events: (info.events || []).slice(),
      context: function () { return call({ op: 'context' }) },
      request: function (capability, args) { return call({ op: 'request', capability: capability, args: args || {} }) },
      on: function (event, handler) {
        if (typeof handler !== 'function') throw new Error('canopy.browser.on(event, handler): handler must be a function')
        if (event !== 'context' && (info.events || []).indexOf(event) < 0) throw new Error('this page does not declare the browser event "' + event + '"')
        var set = browserHandlers[event] || (browserHandlers[event] = [])
        set.push(handler)
        return function () { var i = set.indexOf(handler); if (i >= 0) set.splice(i, 1) }
      },
      open: function (u) { return call({ op: 'open', url: u }) },
    }
  }

  function reportError(e) {
    toHost({ type: 'error', message: e && e.message ? e.message : String(e) })
  }

  function runPageAction(id, name, args) {
    var run = pageActions[name]
    Promise.resolve().then(function () {
      if (!run) throw new Error('this page offers no action "' + name + '"')
      return run(args || {})
    }).then(function (data) {
      toHost({ type: 'page-action-result', id: id, ok: true, data: JSON.parse(JSON.stringify(data == null ? null : data)) })
    }, function (e) {
      toHost({ type: 'page-action-result', id: id, ok: false, error: e && e.message ? e.message : String(e) })
    })
  }

  window.addEventListener('message', function (ev) {
    if (ev.source !== window.parent || ORIGINS.indexOf(ev.origin) < 0) return
    if (parentOrigin && ev.origin !== parentOrigin) return
    parentOrigin = ev.origin
    var msg = ev.data
    if (!msg || typeof msg !== 'object' || msg.channel !== CHANNEL || typeof msg.type !== 'string') return
    if (msg.type === 'render') {
      document.documentElement.classList.toggle('dark', msg.theme === 'dark')
      browserHandlers = {}
      context = Object.assign({}, msg.app || {}, { pages: (msg.bundle && msg.bundle.pages) || [] })
      canopy.app = msg.app ? appApi(msg.app) : null
      canopy.browser = msg.browser ? browserApi(msg.browser) : null
      canopy.scope = msg.scope || null
      resolveReady({ app: canopy.app, scope: canopy.scope, browser: canopy.browser })
      try { window.dispatchEvent(new CustomEvent('canopy:ready', { detail: { app: canopy.app } })) } catch (e) { reportError(e) }
    } else if (msg.type === 'agent-task-status') {
      try { if (statusListeners[msg.id]) statusListeners[msg.id](msg.task) } catch (e) { reportError(e) }
    } else if (msg.type === 'browser-event') {
      (browserHandlers[msg.event] || []).slice().forEach(function (h) {
        try { h(msg.data) } catch (e) { reportError(e) }
      })
    } else if (msg.type === 'page-action') {
      runPageAction(msg.id, msg.name, msg.args)
    } else if (msg.type === 'read-result') {
      var waiter = pending[msg.id]
      if (!waiter) return
      delete pending[msg.id]
      if (msg.ok) waiter.resolve(msg.data)
      else waiter.reject(rejection(msg))
    }
  })

  // Links: another file of the bundle is another page of the app; anything else leaves through
  // the canopy page, which confirms an outside address. The frame never navigates itself.
  document.addEventListener('click', function (ev) {
    var a = ev.target && ev.target.closest ? ev.target.closest('a[href]') : null
    if (!a || ev.defaultPrevented) return
    var href = a.getAttribute('href') || ''
    if (/^(blob:|data:|#|javascript:)/i.test(href) || a.hasAttribute('download')) return
    ev.preventDefault()
    var to = resolvePage(href)
    if (to) toHost({ type: 'go', page: to.page, params: to.params, newTab: a.target === '_blank' })
    else if (/^(https?:|mailto:)/i.test(a.href) && new URL(a.href, location.href).pathname.indexOf(ROOT) !== 0) canopy.navigate(a.href)
    else reportError(new Error('this app has no page at "' + href + '"'))
  })

  // window.open has no popups to open here: route it like a link.
  window.open = function (target) {
    var to = target ? resolvePage(String(target)) : null
    if (to) toHost({ type: 'go', page: to.page, params: to.params, newTab: true })
    else if (target) canopy.navigate(String(target))
    return null
  }

  window.addEventListener('error', function (ev) { reportError(ev.error || ev.message) })
  window.addEventListener('unhandledrejection', function (ev) { reportError(ev.reason) })

  var last = -1
  function report() {
    var height = Math.ceil(document.documentElement.scrollHeight)
    if (height === last) return
    last = height
    toHost({ type: 'resize', height: height })
  }

  Object.defineProperty(window, 'canopy', { value: canopy, writable: false, configurable: false })
  if (window.self === window.top) return
  toHost({ type: 'ready' })
  function watch() {
    new ResizeObserver(report).observe(document.documentElement)
    new MutationObserver(report).observe(document.documentElement, { childList: true, subtree: true, attributes: true, characterData: true })
    report()
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', watch)
  else watch()
})()
