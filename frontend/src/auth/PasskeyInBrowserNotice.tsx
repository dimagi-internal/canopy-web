import { ExternalLink } from 'lucide-react'

import { openInBrowser } from './passkeyHere'

/**
 * In place of a form whose last step needs a passkey, where no passkey can run
 * (`passkeysWorkHere()` is false: the Canopy Runner app's web view, or a session
 * minted from a token). Letting the person fill it in only for the final click to
 * be refused wastes the typing — and for a secret, has them paste it for nothing.
 * Instead: say so before they start, and open this same page in their browser.
 */
export function PasskeyInBrowserNotice({ what }: {
  /** What needs the passkey, as a sentence's subject: "Adding a secret". */
  what: string
}) {
  return (
    <div
      className="space-y-2 rounded-md border border-border bg-muted/40 p-3 text-[13px]"
      data-testid="passkey-in-browser"
    >
      <p className="text-foreground">
        {what} needs your passkey, and passkeys don&rsquo;t work inside the Canopy Runner app.
        Open this page in your browser to do it there.
      </p>
      <button
        type="button"
        onClick={() => openInBrowser(window.location.href)}
        className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-primary-foreground"
      >
        <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
        Open in browser
      </button>
    </div>
  )
}
