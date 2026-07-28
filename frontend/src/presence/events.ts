/**
 * Window-level event name used to tell the app shell's presence socket
 * (mounted in AppLayout) that the user just changed their visibility
 * preference on the Settings page, so it should reconnect and re-enter
 * under the new preference immediately rather than waiting for the next
 * navigation.
 *
 * A plain window CustomEvent rather than prop drilling or a context
 * provider: SettingsPage and AppLayout have no other relationship, and
 * this is a one-shot fire-and-forget signal, not shared state.
 */
export const PRESENCE_PREFERENCE_CHANGED_EVENT = 'canopy:presence-preference-changed'
