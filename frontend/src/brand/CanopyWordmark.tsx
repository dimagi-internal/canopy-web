import { CanopyMark } from './CanopyMark'

/** The mark beside the name: what every header shows where "Canopy." used to be. */
export function CanopyWordmark() {
  return (
    <span className="inline-flex items-center gap-2">
      <CanopyMark className="h-[0.9em] w-auto text-brand" aria-hidden="true" />
      Canopy
    </span>
  )
}
