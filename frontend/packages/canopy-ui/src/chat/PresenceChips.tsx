import { PenLine } from "lucide-react";

import type { Participant } from "./protocol";
import { initials, personColor } from "./personColor";

interface Props {
  participants: Participant[];
  presenceUserIds: number[];
  /** Who is looking. Required so the row can exclude YOU.
   *
   *  Without it this rendered your own chip and the empty state said "nobody
   *  else here" — copy and logic disagreeing about whether "else" meant
   *  anything. In practice the empty state was unreachable: alone in a session
   *  you saw one chip, which is indistinguishable from being watched by a
   *  stranger. ChatPanel had `currentUserId` the whole time and never passed
   *  it. Caught by the first two-browser e2e this surface ever had. */
  currentUserId: number | null;
  /** Who is typing right now (their live draft is up). Their chip gets a
   *  solid ring in their colour, a pulse, and a pen badge, so it reads from
   *  the header even while you are looking at the transcript. Optional — an
   *  older caller simply gets no typing signal on the chips. */
  typingUserIds?: number[];
}

/** How many faces before collapsing into "+N". Beyond a handful the row stops
 *  being a glance and starts being a list, and it shares a thin header bar. */
const MAX_FACES = 4;

export function PresenceChips({
  participants,
  presenceUserIds,
  currentUserId,
  typingUserIds = [],
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

  const faces = present.slice(0, MAX_FACES);
  const overflow = present.length - faces.length;

  return (
    <div className="flex items-center gap-2" data-testid="presence-chips">
      <ul
        className="flex items-center -space-x-1.5"
        aria-label={describe(present, typingUserIds)}
        data-testid="presence-list"
      >
        {faces.map((p) => {
          const color = personColor(p.user_id);
          const typing = typingUserIds.includes(p.user_id);
          return (
            <li
              key={p.user_id}
              data-testid="presence-chip"
              data-user-id={p.user_id}
              data-typing={typing ? "true" : undefined}
              title={typing ? `${p.display_name} is typing` : p.display_name}
              className={[
                "relative flex h-7 w-7 items-center justify-center rounded-full",
                "text-[11px] font-semibold ring-2",
                "transition-transform hover:z-10 hover:scale-110",
                color.avatar,
                // Typing: a solid ring in their colour, lifted above its
                // neighbours so the overlap never hides it.
                typing ? `z-10 ${color.ring}` : "ring-background",
              ].join(" ")}
            >
              {initials(p.display_name)}
              {typing && (
                <>
                  {/* The pulse is decoration; reduced motion drops it and
                      the solid ring + badge still say "typing". */}
                  <span
                    aria-hidden="true"
                    className={`pointer-events-none absolute inset-0 rounded-full ring-2 ${color.ring} opacity-60 motion-safe:animate-ping`}
                  />
                  <span
                    aria-hidden="true"
                    data-testid="presence-typing-badge"
                    className="absolute -bottom-1 -right-1 flex h-3.5 w-3.5 items-center justify-center rounded-full bg-background text-foreground ring-1 ring-border"
                  >
                    <PenLine className="h-2.5 w-2.5" />
                  </span>
                </>
              )}
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
function describe(present: Participant[], typingUserIds: number[]): string {
  const names = present
    .map((p) => (typingUserIds.includes(p.user_id) ? `${p.display_name} (typing)` : p.display_name))
    .join(", ");
  const who = present.length === 1 ? "1 other person here" : `${present.length} other people here`;
  return `${who}: ${names}`;
}
