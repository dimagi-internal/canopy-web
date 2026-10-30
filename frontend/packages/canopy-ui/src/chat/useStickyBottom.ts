import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

/**
 * Sticky-bottom auto-scroll for a streaming message list.
 *
 * Behaviour:
 *  - When the user is at (or within `thresholdPx` of) the bottom of the
 *    scroll container, growth of `dep` (e.g. messages array, streaming
 *    text length) snaps the view to the new bottom.
 *  - When the user has scrolled up to read history, growth does NOT
 *    yank them back. Auto-follow resumes once they scroll back down
 *    near the bottom themselves.
 *
 * ANY move up stops following, however small. Being within `thresholdPx`
 * is only how close counts when coming back DOWN: if it also counted on
 * the way up, the first few pixels of a scroll-up still read as "at the
 * bottom", and a streaming reply snapped the reader back down before
 * their gesture ever got clear of the zone. For the same reason a wheel
 * turned upward stops following at once (`onWheel`), before the scroll
 * event for that gesture has arrived.
 *
 * Exactly at the bottom always follows — that is also where the browser
 * clamps the view when content above shrinks, which is not a move up.
 *
 * Use "instant" scroll behavior for streaming chunks — smooth scroll
 * cannot keep up with high-frequency updates and the view drifts.
 *
 * Returns:
 *  - `containerRef`: attach to the scrollable element.
 *  - `onScroll`: attach to `onScroll` on the same element.
 *  - `onWheel`: attach to `onWheel` on the same element.
 *  - `scrollToBottom`: force a snap (e.g. on send).
 *  - `away`: the user has scrolled up off the bottom (offer "Jump to latest").
 *  - `unseen`: and something new arrived since (say "New messages").
 */
export function useStickyBottom<T>(
  dep: T,
  options: { thresholdPx?: number; enabled?: boolean } = {},
): {
  containerRef: RefObject<HTMLDivElement | null>;
  onScroll: () => void;
  onWheel: (e: { deltaY: number }) => void;
  scrollToBottom: () => void;
  away: boolean;
  unseen: boolean;
} {
  const { thresholdPx = 100, enabled = true } = options;
  const containerRef = useRef<HTMLDivElement>(null);
  // Default true: when the container first mounts there's no history
  // to read, so the user is "at the bottom" by definition.
  const followRef = useRef(true);
  // Where the last scroll event left the view, to tell a move up from down.
  const lastTopRef = useRef(0);
  const [away, setAway] = useState(false);
  const [unseen, setUnseen] = useState(false);

  const setFollow = useCallback((follow: boolean) => {
    followRef.current = follow;
    setAway(!follow);
    if (follow) setUnseen(false);
  }, []);

  const onScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const fromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const movedUp = el.scrollTop < lastTopRef.current;
    lastTopRef.current = el.scrollTop;
    if (fromBottom <= 1) setFollow(true);
    else if (movedUp) setFollow(false);
    else setFollow(fromBottom < thresholdPx);
  }, [thresholdPx, setFollow]);

  const onWheel = useCallback((e: { deltaY: number }) => {
    if (e.deltaY < 0) setFollow(false);
  }, [setFollow]);

  const scrollToBottom = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    lastTopRef.current = el.scrollTop;
    setFollow(true);
  }, [setFollow]);

  useEffect(() => {
    if (!enabled) return;
    const el = containerRef.current;
    if (!el) return;
    if (followRef.current) {
      // "instant" by direct scrollTop write — smooth scroll falls
      // behind during streaming and the view drifts.
      el.scrollTop = el.scrollHeight;
      lastTopRef.current = el.scrollTop;
    } else {
      // Not followed, so say it: the person is reading history and something
      // new landed below them.
      setUnseen(true);
    }
    // dep is the signal for "something new arrived" (it also drives
    // `unseen`); it should change whenever the message list grows (length or
    // the last message's content length). Growth with no new row is followed
    // by the observer below.
  }, [dep, enabled]);

  // Follow the content's HEIGHT, not only `dep`. Plenty changes what is on
  // screen without changing `dep`: the socket's snapshot replacing the tab's
  // cached copy of a chat (both are the 60-row tail, often ending on a tool row
  // with no text, so `dep` reads identical), a finished turn folding, markdown,
  // images and Views laying out after the render. Each left a following view
  // short of the bottom with nothing to correct it — no scroll event fires when
  // content grows below, so not even "Jump to latest" appeared — until some
  // later change to `dep` (e.g. "Load all messages") happened to snap it.
  useEffect(() => {
    if (!enabled) return;
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const snap = () => {
      if (!followRef.current) return;
      el.scrollTop = el.scrollHeight;
      lastTopRef.current = el.scrollTop;
    };
    const resize = new ResizeObserver(snap);
    resize.observe(el);
    for (const child of Array.from(el.children)) resize.observe(child);
    // The list's root can be swapped (empty state → messages); watch whatever
    // the container holds now.
    const children = typeof MutationObserver === "undefined" ? null : new MutationObserver((records) => {
      for (const r of records) {
        r.addedNodes.forEach((n) => {
          if (n instanceof Element) resize.observe(n);
        });
      }
      snap();
    });
    children?.observe(el, { childList: true });
    return () => {
      resize.disconnect();
      children?.disconnect();
    };
  }, [enabled]);

  return { containerRef, onScroll, onWheel, scrollToBottom, away, unseen };
}
