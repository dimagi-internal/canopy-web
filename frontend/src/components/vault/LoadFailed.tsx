/** A section of the vault page could not read its data. Said plainly, so the page never shows an
 *  empty table or a "not made yet" where the truth is "we could not look". */
export function LoadFailed({ what }: { what: string }) {
  return (
    <p role="alert" className="text-[12px] text-destructive" data-testid="load-failed">
      Could not load {what}. Reload the page to try again.
    </p>
  )
}
