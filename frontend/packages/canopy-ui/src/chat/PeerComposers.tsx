import { useLayoutEffect, useRef, useState } from "react";

import type { PeerDraft } from "./protocol";
import { initials, personColor } from "./personColor";

/** Everyone else's box, live — the multiplayer half of the composer.
 *
 *  Each person typing gets a read-only copy of a composer, stacked directly
 *  above your own and drawn with the same width and corners, so what you see is
 *  "another person writing", not a status line. It used to be one row of grey
 *  italic `text-xs`, truncated to a single line — in a live test it was easy to
 *  miss entirely and read like the app reporting on itself.
 *
 *  Newest edit last (the order `peer_drafts` arrives in). A person in `typing`
 *  mode shares the fact, not the words: their box shows a typing indicator
 *  instead of text. Motion (the caret blink, the dots) is `motion-safe` only;
 *  under reduced motion the caret and dots are still drawn, just still. */
export function PeerComposers({ peers }: { peers: PeerDraft[] }) {
  if (peers.length === 0) return null;
  return (
    <ul className="mb-2 space-y-1.5" aria-live="polite" data-testid="peer-composers">
      {peers.map((p) => (
        <PeerComposer key={p.author.id} peer={p} />
      ))}
    </ul>
  );
}

/** How tall a live draft may grow before it caps: ~3 lines of `text-sm`. */
const MAX_BODY = "max-h-[3.75rem]";

function PeerComposer({ peer }: { peer: PeerDraft }) {
  const color = personColor(peer.author.id);
  const withheld = peer.body === "";
  const bodyRef = useRef<HTMLDivElement>(null);
  const textRef = useRef<HTMLParagraphElement>(null);
  const [overflowing, setOverflowing] = useState(false);

  // The box is anchored to the BOTTOM of the text: the newest words are the
  // ones being written, so a long draft shows its tail and fades out at the
  // top, rather than freezing on its first three lines while the person keeps
  // typing below them.
  useLayoutEffect(() => {
    const box = bodyRef.current;
    const text = textRef.current;
    if (!box || !text) return setOverflowing(false);
    setOverflowing(text.scrollHeight > box.clientHeight + 1);
  }, [peer.body]);

  return (
    <li
      data-testid="peer-composer"
      data-user-id={peer.author.id}
      className={`rounded-md border border-l-4 border-input ${color.edge} bg-card px-2 py-1.5 shadow-sm`}
    >
      <div className="flex min-w-0 items-center gap-1.5 text-xs">
        <span
          aria-hidden="true"
          className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[9px] font-semibold ${color.avatar}`}
        >
          {initials(peer.author.name)}
        </span>
        <span className={`truncate font-semibold ${color.text}`}>{peer.author.name}</span>
        {/* A real space, so the text (and a screen reader) says "Robin Sharma
            is typing" — flex `gap` is only visual. */}
        {" "}
        <span className="shrink-0 text-muted-foreground">is typing</span>
      </div>
      {withheld ? (
        <div className="mt-1 flex h-5 items-center pl-6" data-testid="peer-typing-indicator">
          <TypingDots />
        </div>
      ) : (
        <div
          ref={bodyRef}
          className={`mt-1 flex ${MAX_BODY} flex-col justify-end overflow-hidden ${
            overflowing ? "[mask-image:linear-gradient(to_bottom,transparent,black_1.25rem)]" : ""
          }`}
        >
          <p
            ref={textRef}
            data-testid="peer-composer-text"
            className="whitespace-pre-wrap text-sm text-foreground [overflow-wrap:anywhere]"
          >
            {peer.body}
            <span
              aria-hidden="true"
              data-testid="peer-caret"
              className={`ml-px inline-block h-[1.1em] w-0.5 translate-y-[0.2em] rounded-sm bg-current ${color.text} motion-safe:animate-pulse`}
            />
          </p>
        </div>
      )}
    </li>
  );
}

function TypingDots() {
  return (
    <span className="inline-flex gap-1 text-muted-foreground" aria-hidden="true">
      <span className="h-1.5 w-1.5 rounded-full bg-current motion-safe:animate-bounce motion-safe:[animation-delay:-0.3s]" />
      <span className="h-1.5 w-1.5 rounded-full bg-current motion-safe:animate-bounce motion-safe:[animation-delay:-0.15s]" />
      <span className="h-1.5 w-1.5 rounded-full bg-current motion-safe:animate-bounce" />
    </span>
  );
}
