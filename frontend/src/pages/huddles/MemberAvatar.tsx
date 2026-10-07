import { initial } from './huddleModel'

/** A participant's initial in a ring of its own colour. */
export function MemberAvatar({ slug, hue, size = 'md' }: { slug: string; hue: string; size?: 'sm' | 'md' }) {
  return (
    <span
      aria-hidden
      className={
        'inline-flex shrink-0 items-center justify-center rounded-full font-semibold ring-1 ring-inset ' +
        (size === 'sm' ? 'size-6 text-[11px]' : 'size-9 text-[14px]')
      }
      style={{
        color: hue,
        background: `color-mix(in oklch, ${hue} 16%, transparent)`,
        // ring colour via a CSS var Tailwind's ring utility reads
        ['--tw-ring-color' as string]: `color-mix(in oklch, ${hue} 45%, transparent)`,
      }}
    >
      {initial(slug)}
    </span>
  )
}
