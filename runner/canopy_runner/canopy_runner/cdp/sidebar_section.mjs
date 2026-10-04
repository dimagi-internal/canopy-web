// Which "Open task <task>" row in emdash's sidebar belongs to project <project>?
//
// emdash task names are unique per PROJECT, not per emdash. Two agents can each
// have a task called "editing", and the sidebar then holds two buttons with the
// identical aria-label "Open task editing". The old lookup clicked the FIRST one in
// DOM order, so a message meant for eva's "editing" was typed into ada's — four
// times, because the runner then watched eva's transcript, saw nothing, and the
// turn was retried (turn 22662f53, 2026-10-04).
//
// The sidebar is a FLAT list (measured on emdash, 2026-10-04): each project's
// header row carries a "New task for <project>" button, and that project's task
// rows follow it until the next header. So a task row belongs to the nearest
// header ABOVE it. This module is that rule, over the ordered list of sidebar
// labels — pure, so it can be tested without a live emdash.
//
// The list is VIRTUALIZED: rows scrolled out of view are not in the DOM. The caller
// scrolls down through the section and passes `carried = true` once it has seen
// the header, so that when the header itself has scrolled out of the DOM the rows
// still rendered above the next header are known to be this project's.

export const HEADER_PREFIX = 'New task for ';
export const TASK_PREFIX = 'Open task ';

/** True for the labels this module reasons about (headers and task rows). */
export function isSidebarLabel(label) {
  return typeof label === 'string'
    && (label.startsWith(HEADER_PREFIX) || label.startsWith(TASK_PREFIX));
}

/**
 * @param {string[]} labels  sidebar labels in DOM order (headers and task rows)
 * @param {string} project   the emdash project that owns the task
 * @param {string} task      the task name
 * @param {boolean} carried  the header was seen on an earlier (scrolled-past) read
 * @returns {{state: 'found', index: number} | {state: 'ended'|'more'|'before'}}
 *   found  — `labels[index]` is this project's row for `task`
 *   ended  — the section was read to its end (the next header) without a match
 *   more   — inside the section, its end not rendered yet: scroll down and re-read
 *   before — the section's header was not reached
 */
export function pickInSection(labels, project, task, carried = false) {
  const header = HEADER_PREFIX + project;
  const want = TASK_PREFIX + task;
  // With the header in view, start from scratch: rows ABOVE it are another
  // project's even if we were already inside this section on a previous read.
  let inSection = labels.includes(header) ? false : Boolean(carried);
  for (let i = 0; i < labels.length; i++) {
    const label = labels[i];
    if (label.startsWith(HEADER_PREFIX)) {
      if (label === header) { inSection = true; continue; }
      if (inSection) return { state: 'ended' };
      continue;
    }
    if (inSection && label === want) return { state: 'found', index: i };
  }
  return { state: inSection ? 'more' : 'before' };
}
