ObjC.import('AppKit');

// Open a claude:// URL, then hand focus straight back to whatever was frontmost.
//   osascript -l JavaScript desktop_quiet_open.js <url> [watch-seconds]
//
// The app's own `open-url` handler calls show() + focus() on its main window for
// every claude:// link it handles, whatever the opener asks; NSWorkspace's
// `activates = false` and `open -g` are both overridden (canopy-web#1188, "Focus
// round 2"). So this does not prevent the activation, it undoes it: it watches
// for Claude becoming frontmost and re-activates the app the person was in,
// re-hiding Claude if it was hidden. Measured on haldimagi: Claude held the front
// ~60ms per open. A mitigation, not a fix: a keystroke typed in that window can
// land in Claude.
//
// It acts on the FIRST activation only, and never when Claude was already the
// frontmost app (the person is using it; the link just navigates).
function run(argv) {
  const BUNDLE = 'com.anthropic.claudefordesktop';
  const ws = $.NSWorkspace.sharedWorkspace;
  const prev = ws.frontmostApplication;
  const prevId = prev.isNil() ? '' : ObjC.unwrap(prev.bundleIdentifier) || '';
  const apps = $.NSRunningApplication.runningApplicationsWithBundleIdentifier(BUNDLE);
  const wasHidden = apps.count > 0 ? apps.objectAtIndex(0).hidden : true;
  const cfg = $.NSWorkspaceOpenConfiguration.configuration;
  cfg.activates = false;
  const t0 = Date.now();
  ws.openURLConfigurationCompletionHandler($.NSURL.URLWithString(argv[0]), cfg, (app, err) => {});
  if (!prevId || prevId === BUNDLE) return `opened; left focus alone (frontmost was ${prevId || 'nothing'})`;
  const until = t0 + 1000 * Number(argv[1] || 3);
  while (Date.now() < until) {
    $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.005));
    const front = ws.frontmostApplication;
    if (!front.isNil() && ObjC.unwrap(front.bundleIdentifier) === BUNDLE) {
      prev.activateWithOptions(0);
      if (wasHidden) front.hide;
      return `opened; gave focus back to ${prevId} after ${Date.now() - t0}ms (re-hid Claude: ${wasHidden})`;
    }
  }
  return `opened; Claude did not come forward within the watch (frontmost stayed ${prevId})`;
}
