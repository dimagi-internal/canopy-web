// emdash CDP control sidecar — drives the RUNNING emdash app over the Chrome
// DevTools Protocol (emdash is Electron; launch it with --remote-debugging-port).
// This is the sanctioned path (no DB injection, no app patching): tasks created
// here flow through emdash's own UI, so they appear live in the sidebar and run
// interactive `claude` on the subscription. Commands (arg = JSON on argv[3]):
//   list                              -> {ok, tasks:[names], projects:[names]}
//   create {project, prompt}          -> {ok, action:"created"}    (new session)
//   open-send {task, text}            -> {ok, action:"sent", task} (REUSE existing)
//                                        -> {ok, action:"collision", line} if the prompt
//                                           already holds UNSENT text (human was typing when
//                                           emdash switched tasks) — does NOT clobber it.
//   open-send {task, text, clearFirst}-> {ok, action:"sent-cleared"} kills the current input
//                                           line first, then sends (the human's "Clear & send").
//   interrupt {task}                  -> {ok, task} opens the task (same lookup as open-send)
//                                           and presses Escape — Claude Code's TUI treats
//                                           this as "stop the running turn" (see runner.cancel).
// Text is delivered via CDP Input.insertText (one atomic commit, not char-by-char
// typing) so it lands fast and narrows the window for a keystroke collision.
// All output is a single JSON line on stdout. Occlusion-proof: uses JS-dispatched
// clicks so it works while emdash is backgrounded (no foreground focus needed).
import { chromium } from 'playwright-core';

const command = process.argv[2];
const args = JSON.parse(process.argv[3] || '{}');
const port = args.port || 9222;

function out(o) { process.stdout.write(JSON.stringify(o)); }
function fail(msg) { out({ ok: false, error: msg }); process.exit(1); }

let browser;
try {
  browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
} catch {
  fail(`cannot connect to emdash CDP on 127.0.0.1:${port} — launch emdash with --remote-debugging-port=${port}`);
}
const page = browser.contexts()[0]?.pages()[0];
if (!page) fail('no emdash renderer page found over CDP');

// The sidebar VIRTUALIZES rows — anything scrolled out of view is not in the DOM at
// all, so a one-shot querySelector can't tell "absent" from "off-screen". Scan the
// scroller (.overflow-y-auto) top→bottom, letting rows render, until `label` appears.
// Both create ("New task for X") and open-send ("Open task X") need this; open-send
// not having it is what made live sessions look deleted and get duplicated.
const scrollToFind = async (label) => {
  const scrollBy = (val) => page.evaluate((v) => {
    const sc = [...document.querySelectorAll('.overflow-y-auto')].sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
    if (!sc) return 0;
    if (v === 'top') sc.scrollTop = 0; else sc.scrollTop += v;
    return sc.scrollTop;
  }, val);
  const find = () => page.evaluate((l) => {
    const btn = [...document.querySelectorAll('button')].find(x => x.getAttribute('aria-label') === l);
    if (btn) { btn.scrollIntoView({ block: 'center' }); return true; }
    return false;
  }, label);
  await scrollBy('top');
  await page.waitForTimeout(200);
  let lastTop = -1;
  for (let i = 0; i < 40; i++) {
    if (await find()) return true;
    const top = await scrollBy(280);          // step down
    await page.waitForTimeout(160);
    if (top === lastTop) break;               // reached the bottom
    lastTop = top;
  }
  return await find();
};

const clickLabel = (label) => page.evaluate((l) => {
  const btn = [...document.querySelectorAll('button')].find(x => x.getAttribute('aria-label') === l);
  if (!btn) return false; btn.click(); return true;
}, label);

// Shared REUSE lookup: find `task` in the (virtualized) sidebar, open it, and focus its
// live terminal input. Used by both open-send (which then reads/inserts text) and
// interrupt (which just needs the terminal focused so Escape lands in the right pane).
// Fails (via `fail`, which exits the process) on any step that can't be completed —
// callers never see a partial/ambiguous state.
const openTask = async (task) => {
  const found = await scrollToFind(`Open task ${task}`);
  // TASK_NOT_FOUND is a claim about the WHOLE sidebar, only trustworthy now that we
  // scan all of it. Even so the caller cross-checks it against emdash's sqlite before
  // creating anything, and any LATER failure here means the task exists but the
  // interaction glitched — the caller must NOT create a duplicate.
  if (!found) fail(`TASK_NOT_FOUND: no task "${task}" in this emdash (archived, or another macOS account)`);
  if (!await clickLabel(`Open task ${task}`)) {
    fail(`could not click task "${task}" after locating it in the sidebar`);
  }
  await page.waitForTimeout(1200);
  // Focus the ACTIVE terminal's input. xterm's real input is an off-screen
  // `.xterm-helper-textarea`, so a Playwright .click() fails its viewport check — we
  // focus it via JS (viewport-agnostic) instead, picking the visible xterm (the active
  // task's pane) when several are mounted.
  const focused = await page.evaluate(() => {
    const terms = [...document.querySelectorAll('.xterm')]
      .filter(t => t.offsetParent !== null && t.getBoundingClientRect().width > 0);
    const term = terms[0];
    const ta = (term && term.querySelector('.xterm-helper-textarea'))
      || document.querySelector('textarea[aria-label="Terminal input"]');
    if (!ta) return false;
    ta.focus();
    return true;
  });
  if (!focused) fail(`could not focus the terminal input for task "${task}"`);
};

try {
  if (command === 'list') {
    const data = await page.evaluate(() => {
      const labels = [...document.querySelectorAll('button')].map(b => b.getAttribute('aria-label') || '');
      return {
        tasks: labels.filter(t => t.startsWith('Open task ')).map(t => t.slice('Open task '.length)),
        projects: labels.filter(t => t.startsWith('New task for ')).map(t => t.slice('New task for '.length)),
      };
    });
    out({ ok: true, ...data });

  } else if (command === 'create') {
    const { project, prompt, taskName } = args;
    const taskNames = () => page.evaluate(() =>
      [...document.querySelectorAll('button')].map(b => b.getAttribute('aria-label') || '')
        .filter(t => t.startsWith('Open task ')).map(t => t.slice('Open task '.length)));
    if (!await scrollToFind(`New task for ${project}`)) {
      fail(`no "New task for ${project}" control — project "${project}" not found in the emdash sidebar`);
    }
    await clickLabel(`New task for ${project}`);
    await page.waitForTimeout(1000);
    // Set a deterministic task NAME so we don't have to detect it afterward (the
    // list-diff is unreliable under sidebar virtualization). The name input is the
    // dialog's first text input (its placeholder is emdash's auto-generated name).
    let finalName = taskName || "";
    if (taskName) {
      const named = await page.evaluate((name) => {
        const dlg = document.querySelector('[role=dialog],[class*=Dialog],[class*=modal]');
        if (!dlg) return false;
        const input = [...dlg.querySelectorAll('input')].find(i => (i.type === 'text' || !i.type) && i.value !== 'claude' && i.value !== 'on');
        if (!input) return false;
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(input, name);                                   // React-friendly value set
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      }, taskName);
      if (!named) finalName = "";   // fell back — will diff below
    }
    const before = await taskNames();
    // Initial-conversation prompt is a contenteditable in the Create Task dialog.
    const ce = page.locator('[role=dialog] [contenteditable="true"], [class*=Dialog] [contenteditable="true"]').first();
    await ce.click();
    await page.keyboard.insertText(prompt);   // atomic commit, not char-by-char
    const created = await page.evaluate(() => {
      const dlg = document.querySelector('[role=dialog],[class*=Dialog],[class*=modal]');
      if (!dlg) return false;
      const btn = [...dlg.querySelectorAll('button')].find(b => /create/i.test(b.textContent || '') && !/close|cancel/i.test(b.textContent || ''));
      if (!btn) return false; btn.click(); return true;
    });
    if (!created) fail('could not find the Create button in the New Task dialog');
    await page.waitForTimeout(3000);
    if (!finalName) {
      // Fallback: diff (best-effort; may be imprecise under virtualization).
      const after = await taskNames();
      const beforeSet = new Set(before);
      finalName = (after.filter(n => !beforeSet.has(n)))[0] || "";
    }
    out({ ok: true, action: 'created', task: finalName });

  } else if (command === 'open-send') {
    // REUSE: open an EXISTING task and send text into its live terminal. Scroll the
    // virtualized sidebar to reach it — a one-shot query only sees the ~visible rows,
    // so with dozens of tasks the target is usually absent from the DOM despite being
    // live (observed 2026-07-15: eva's org-research session, present in emdash's DB,
    // reported TASK_NOT_FOUND and duplicated).
    const { task, text, clearFirst } = args;
    await openTask(task);

    // Read whatever is ALREADY sitting in the prompt. claude's TUI renders its input
    // inside a bordered box; the input line is the last visible row carrying a '>'
    // prompt marker. A non-empty line here means the human was typing when emdash
    // switched to this task and their keystrokes leaked in — a COLLISION we must NOT
    // clobber blindly. Heuristic + version-fragile: on any ambiguity it returns '',
    // which takes the fast send path (never worse than the pre-collision behaviour, and
    // no false-positive dialog).
    const readLine = () => page.evaluate(() => {
      const terms = [...document.querySelectorAll('.xterm')]
        .filter(t => t.offsetParent !== null && t.getBoundingClientRect().width > 0);
      const term = terms[0];
      if (!term) return '';
      const rows = [...term.querySelectorAll('.xterm-rows > div')]
        .map(r => (r.textContent || '').replace(/\u00a0/g, ' '));
      for (let i = rows.length - 1; i >= 0 && i >= rows.length - 14; i--) {
        const m = rows[i].match(/(?:^|[│|])\s*>\s?(.*)$/);
        if (m) return (m[1] || '').replace(/[│|─╭╮╰╯]/g, '').trim();
      }
      return '';
    });
    // Ghost/placeholder hints claude shows in an EMPTY input — treat as empty so the
    // fast path still fires instead of popping a spurious collision dialog.
    const PLACEHOLDER = /^(Try |Ask |\/ for |\? for )/i;
    const isEmpty = (s) => !s || PLACEHOLDER.test(s);

    if (clearFirst) {
      // The human chose "Clear & send": deterministically empty the input, then send.
      // Ctrl+U (kill-to-start) as a fast path, then backspace the MEASURED content and
      // re-read until empty (self-correcting; robust to whatever line editing claude's
      // TUI actually honors). Leaked text is "the last few words", so this is short.
      await page.keyboard.press('Control+U');
      for (let i = 0; i < 6; i++) {
        const n = Math.min((await readLine()).length, 300);
        if (n === 0) break;
        await page.keyboard.press('End');
        for (let k = 0; k < n; k++) await page.keyboard.press('Backspace');
      }
      await page.keyboard.insertText(text);
      await page.keyboard.press('Enter');
      out({ ok: true, action: 'sent-cleared', task });
    } else {
      const line = await readLine();
      if (!isEmpty(line)) {
        // Don't touch it — hand the collision back to the runner to ask the human.
        out({ ok: true, action: 'collision', task, line });
      } else {
        await page.keyboard.insertText(text);   // atomic commit, not char-by-char
        await page.keyboard.press('Enter');
        out({ ok: true, action: 'sent', task });
      }
    }

  } else if (command === 'interrupt') {
    // Cancel: open the task exactly as open-send does (find + focus its terminal),
    // but instead of inserting text just press Escape — Claude Code's TUI treats Esc
    // as "stop the current turn" mid-flight.
    const { task } = args;
    await openTask(task);
    await page.keyboard.press('Escape');   // Claude Code: Esc interrupts the running turn
    out({ ok: true, task });

  } else {
    fail(`unknown command: ${command}`);
  }
} catch (e) {
  fail(String((e && e.message) || e));
} finally {
  await browser.close();
}
