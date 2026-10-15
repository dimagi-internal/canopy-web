import { useRef } from 'react'
import { withBase } from '@/lib/basePath'

/**
 * One cut's video, with a poster that shows something.
 *
 * A recorded cut usually opens on a blank frame (the page is still painting
 * when recording starts), and `preload="metadata"` shows frame 0 — so cut 1 of
 * the RUTF demo read as an empty white box. Without a `poster` we nudge the
 * playhead to a frame a little way in once metadata arrives, and put it back to
 * the start on the first play so no words are skipped. A `#t=` media fragment
 * would do the first half but START playback there, cutting the opening line.
 *
 * `onDuration` reports the measured length, so a page can show it even when
 * nobody recorded `duration_sec` at upload.
 */
export function CutPlayer({
  src,
  poster,
  label,
  className,
  onDuration,
}: {
  src: string
  poster?: string | null
  label: string
  className?: string
  onDuration?: (sec: number) => void
}) {
  const nudged = useRef(false)
  const played = useRef(false)
  return (
    <video
      src={withBase(src)}
      poster={poster ? withBase(poster) : undefined}
      controls
      playsInline
      preload="metadata"
      aria-label={`Demo video: ${label}`}
      className={className ?? 'max-h-[50vh] w-full bg-black'}
      onLoadedMetadata={(e) => {
        const v = e.currentTarget
        if (Number.isFinite(v.duration) && v.duration > 0) onDuration?.(v.duration)
        if (!poster && !played.current && v.currentTime === 0 && Number.isFinite(v.duration)) {
          nudged.current = true
          // The RUTF cut 1 recording is blank white for its first ~2 s.
          v.currentTime = Math.min(3, v.duration / 4)
        }
      }}
      onPlay={(e) => {
        if (!played.current && nudged.current) e.currentTarget.currentTime = 0
        played.current = true
      }}
    />
  )
}

export function fmtDuration(sec?: number | null): string | null {
  if (sec == null || !Number.isFinite(sec) || sec <= 0) return null
  const s = Math.round(sec)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export function countWords(text: string): number {
  return (text || '').split(/\s+/).filter(Boolean).length
}
