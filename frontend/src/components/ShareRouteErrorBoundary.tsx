import { isRouteErrorResponse, useRouteError } from 'react-router-dom'

/**
 * Error boundary for the public `/w/:workspace/share/:token` viewer ONLY.
 *
 * `/w/:workspace/share/:token` (`SessionSharePage`) is mounted OUTSIDE `AppLayout` and is
 * deliberately light-themed + chrome-less — a Dimagi login is never required
 * to read it (see CLAUDE.md's design-tokens exception). The app's default
 * `RouteErrorBoundary` is wrong here on three counts: it styles off the dark
 * app token set (theme-inconsistent on a page that intentionally opts out of
 * the token set), its copy says "use the navigation" on a page with no nav,
 * and its "Back to Canopy" link sends an anonymous visitor into the
 * login-gated app. This boundary mirrors `SessionSharePage`'s own literal
 * zinc/white palette instead of the semantic tokens, and offers only what
 * actually helps a link-holder: reload, or copy the link to try again later.
 */
export function ShareRouteErrorBoundary() {
  const error = useRouteError()

  const message = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? error.message
      : typeof error === 'string'
        ? error
        : 'An unexpected error occurred.'

  const stack = error instanceof Error ? error.stack : undefined

  return (
    <div className="flex min-h-screen items-center justify-center bg-white px-4">
      <div className="w-full max-w-md rounded-lg border border-zinc-200 bg-white p-6 text-center shadow-sm">
        <h1 className="text-lg font-medium text-zinc-900">This shared session didn’t load.</h1>
        <p className="mt-1 text-sm text-zinc-500">
          The link may be broken, or something went wrong while loading it. Try reloading — if it
          keeps happening, ask whoever sent you the link to re-share it.
        </p>

        <p className="mt-4 break-words rounded border border-zinc-200 bg-zinc-50 px-2 py-1.5 font-mono text-xs text-zinc-500">
          {message}
        </p>

        <button
          type="button"
          onClick={() => window.location.reload()}
          className="mt-4 rounded bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-zinc-800"
        >
          Try again
        </button>

        {import.meta.env.DEV && stack && (
          <details className="mt-4 text-left">
            <summary className="cursor-pointer text-xs text-zinc-500">
              Stack trace (dev only)
            </summary>
            <pre className="mt-2 overflow-x-auto rounded border border-zinc-200 bg-zinc-50 p-2 text-[10px] text-zinc-500">
              {stack}
            </pre>
          </details>
        )}
      </div>
    </div>
  )
}
