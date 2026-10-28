/**
 * Open a `slack://` deep link (a session's Slack `app_url`, built by
 * apps/slack/location.py::app_url) in the Slack desktop app. The browser hands a
 * custom scheme to the app and stays on this page; with no app installed nothing
 * happens, which is why every such link sits beside the web-client one.
 */
export function openInSlackApp(appUrl: string) {
  window.location.assign(appUrl)
}
