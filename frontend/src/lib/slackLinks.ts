/**
 * Open a `slack://` deep link (a session's Slack `app_url`, built by
 * apps/slack/location.py::app_url) in the Slack desktop app. The browser hands a
 * custom scheme to the app and stays on this page; with no app installed nothing
 * happens, which is why every such link sits beside the web-client one.
 */
export function openInSlackApp(appUrl: string) {
  window.location.assign(appUrl)
}

/** What to call a session's Slack thread: `#channel`, never the channel id. */
export function slackLabel(thread: { channel_name?: string; is_dm?: boolean }): string {
  if (thread.is_dm) return 'DM'
  return thread.channel_name ? `#${thread.channel_name.replace(/^#/, '')}` : 'thread'
}

/** Open a session's Slack thread natively (desktop app, or the phone's Slack via the same deep link). */
export function openSlackThread(thread: { app_url?: string; url?: string }) {
  if (thread.app_url) openInSlackApp(thread.app_url)
  else if (thread.url) window.open(thread.url, '_blank', 'noopener,noreferrer')
}
