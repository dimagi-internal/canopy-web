import { useCallback, useEffect, useRef, useState } from 'react'

/** Put `text` on the clipboard. Resolves `false` where the clipboard API is
 *  missing or refused (an insecure origin, a denied permission) instead of
 *  throwing, so the caller decides what to show. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard) return false
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

/** Copy with a "Copied" moment. `copied` is the key of the last successful
 *  copy (so one hook serves a list of rows) until `resetMs` passes; `null`
 *  resetMs keeps it until the next copy. */
export function useCopy(resetMs: number | null = 1500) {
  const [copied, setCopied] = useState<string | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])
  const copy = useCallback(
    async (text: string, key = 'copied'): Promise<boolean> => {
      const ok = await copyText(text)
      if (!ok) return false
      setCopied(key)
      if (timer.current) clearTimeout(timer.current)
      if (resetMs !== null) timer.current = setTimeout(() => setCopied(null), resetMs)
      return true
    },
    [resetMs],
  )
  const reset = useCallback(() => setCopied(null), [])
  return { copied, copy, reset }
}
