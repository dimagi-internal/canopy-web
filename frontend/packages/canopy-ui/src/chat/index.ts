// canopy-ui/chat — a reusable, app-agnostic multiplayer chat kit.
//
// Speaks the canonical chat WebSocket protocol (ace-web's contract; string
// ids). App specifics — the ws URL builder, the markdown renderer, session
// meta — are injected. The presentational tree is props-in / callbacks-out.

// Protocol types
export type {
  Message,
  MessageRole,
  MessageStatus,
  Draft,
  Participant,
  SessionState,
  WsAction,
  WsEvent,
} from "./protocol";

// Reducer (pure)
export { sessionReducer } from "./sessionReducer";
export { prependHistory } from "./history";

// Hooks
export {
  useSessionSocket,
  type UseSessionSocketOptions,
  type UseSessionSocketResult,
} from "./useSessionSocket";
export { useStickyBottom } from "./useStickyBottom";

// Draft idle helpers
export { IDLE_THRESHOLD_MS, isDraftIdle, msUntilDraftIdle } from "./drafts";

// Tool-message pairing helpers
export {
  pairToolMessages,
  deriveToolStatus,
  toolPreview,
  toolDisplayName,
  type ChatRow,
  type ToolCallStatus,
} from "./pairToolMessages";

// Presentational components
export { ChatPanel, type ChatPanelProps } from "./ChatPanel";
export { MessageList } from "./MessageList";
export { MessageItem, type RenderMarkdown } from "./MessageItem";
export { ToolCallPair } from "./ToolCallPair";
export { SendBox } from "./SendBox";
export { PresenceChips } from "./PresenceChips";
export { ConnectionStatus } from "./ConnectionStatus";
export {
  PlacementBanner,
  type PlacementBannerProps,
  type PlacementRunner,
} from "./PlacementBanner";
