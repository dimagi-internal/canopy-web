import type { Participant } from "./protocol";

interface Props {
  participants: Participant[];
  presenceUserIds: number[];
  draftHolderId: number | null;
  draftHolderIdle: boolean;
  /** Who is looking. Required so the row can exclude YOU.
   *
   *  Without it this rendered your own chip and the empty state said "nobody
   *  else here" — copy and logic disagreeing about whether "else" meant
   *  anything. In practice the empty state was unreachable: alone in a session
   *  you saw one chip, which is indistinguishable from being watched by a
   *  stranger. ChatPanel had `currentUserId` the whole time and never passed
   *  it. Caught by the first two-browser e2e this surface ever had. */
  currentUserId: number;
}

/** A stable colour per person, so two people are told apart at a glance.
 *
 *  Every chip used to be `bg-muted`, which meant identity rested entirely on
 *  two initials — and initials collide constantly on a small team (two J.K.s
 *  render identically). Hue is derived from the user id so a given person is
 *  the same colour in every session, for everyone, with no state to keep. */
const PALETTE = [
  "bg-sky-500/20 text-sky-700 dark:text-sky-200 ring-sky-500/40",
  "bg-emerald-500/20 text-emerald-700 dark:text-emerald-200 ring-emerald-500/40",
  "bg-violet-500/20 text-violet-700 dark:text-violet-200 ring-violet-500/40",
  "bg-amber-500/20 text-amber-700 dark:text-amber-200 ring-amber-500/40",
  "bg-rose-500/20 text-rose-700 dark:text-rose-200 ring-rose-500/40",
  "bg-teal-500/20 text-teal-700 dark:text-teal-200 ring-teal-500/40",
];

const colorFor = (userId: number) => PALETTE[Math.abs(userId) % PALETTE.length];

/** How many faces before collapsing into "+N". Beyond a handful the row stops
 *  being a glance and starts being a list, and it shares a thin header bar. */
const MAX_FACES = 4;

export function PresenceChips({
  participants,
  presenceUserIds,
  draftHolderId,
  draftHolderIdle,
  currentUserId,
}: Props) {
  const present = participants.filter(
    (p) => presenceUserIds.includes(p.user_id) && p.user_id !== currentUserId,
  );

  if (present.length === 0) {
    return (
      <div
        className="text-xs text-muted-foreground"
        data-testid="presence-empty"
      >
        just you
      </div>
    );
  }

  const editor = present.find(
    (p) => p.user_id === draftHolderId && !draftHolderIdle,
  );
  const faces = present.slice(0, MAX_FACES);
  const overflow = present.length - faces.length;

  return (
    <div className="flex items-center gap-2" data-testid="presence-chips">
      {/* The one thing worth WORDS rather than a ring: somebody is typing into
          the box you share, and the old UI said so only in a `title` tooltip —
          invisible on touch, invisible to a screen reader, and invisible to
          anyone not hovering the exact 28px circle. */}
      {editor && (
        <span
          className="hidden items-center gap-1 text-xs text-muted-foreground sm:flex"
          data-testid="presence-editing-label"
        >
          <span className="relative flex h-1.5 w-1.5" aria-hidden="true">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-60" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-primary" />
          </span>
          {firstName(editor.display_name)} is typing…
        </span>
      )}
      <ul
        className="flex items-center -space-x-1.5"
        aria-label={describe(present, editor)}
        data-testid="presence-list"
      >
        {faces.map((p) => {
          const isEditor = p.user_id === editor?.user_id;
          return (
            <li
              key={p.user_id}
              data-testid="presence-chip"
              data-user-id={p.user_id}
              data-editing={isEditor ? "true" : "false"}
              title={p.display_name + (isEditor ? " — editing…" : "")}
              className={[
                "flex h-7 w-7 items-center justify-center rounded-full",
                "text-[11px] font-semibold ring-2 ring-background",
                "transition-transform hover:z-10 hover:scale-110",
                colorFor(p.user_id),
                isEditor ? "z-10 !ring-primary" : "",
              ].join(" ")}
            >
              {initials(p.display_name)}
            </li>
          );
        })}
        {overflow > 0 && (
          <li
            data-testid="presence-overflow"
            title={present.slice(MAX_FACES).map((p) => p.display_name).join(", ")}
            className="flex h-7 w-7 items-center justify-center rounded-full bg-muted text-[11px] font-semibold text-muted-foreground ring-2 ring-background"
          >
            +{overflow}
          </li>
        )}
      </ul>
    </div>
  );
}

/** The accessible name for the row — the same fact the faces carry, in words.
 *  A row of coloured circles is meaningless without this. */
function describe(present: Participant[], editor?: Participant): string {
  const names = present.map((p) => p.display_name).join(", ");
  const who = present.length === 1 ? "1 other person here" : `${present.length} other people here`;
  return editor ? `${who}: ${names}. ${editor.display_name} is editing.` : `${who}: ${names}`;
}

function firstName(name: string): string {
  return name.trim().split(/\s+/)[0] || name;
}

function initials(name: string): string {
  return name
    .split(" ")
    .map((w) => w[0])
    .filter(Boolean)
    .join("")
    .slice(0, 2)
    .toUpperCase();
}
