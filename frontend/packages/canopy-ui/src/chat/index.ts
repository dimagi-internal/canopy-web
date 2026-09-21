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
  SessionMenu,
  SessionState,
  TurnStatus,
  WsAction,
  WsEvent,
} from "./protocol";

// REST <-> kit conversion. Shared because both hosts that read a transcript over
// REST were writing this out by hand, against this kit's own Message type.
export { restToKitMessage, type RestMessage } from "./restMessage";

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

// Where the ask stands — the kit's voice for the server-side turn status that
// Slack, this panel and the embedded widget all render from. Exported because
// a host may want the same wording outside the panel (a session list, a
// placement banner) rather than inventing a second vocabulary for it.
export {
  agentHasFloor,
  pendingLabel,
  turnNotice,
  type TurnNotice,
} from "./turnStatus";

// Draft idle helpers
export { IDLE_THRESHOLD_MS, isDraftIdle, msUntilDraftIdle } from "./drafts";

// Composer persistence (survives unmount / tab close)
export {
  DRAFT_STORAGE_TTL_MS,
  clearStoredDraft,
  defaultDraftStorage,
  draftStorageKey,
  readStoredDraft,
  writeStoredDraft,
  type DraftStorage,
} from "./drafts";

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
export { MenuPrompt, type MenuPromptProps } from "./MenuPrompt";
export { MessageList } from "./MessageList";
export { MessageItem, type RenderMarkdown } from "./MessageItem";
export { ToolCallPair } from "./ToolCallPair";
export { SendBox, type PendingAttachment } from "./SendBox";
export { PresenceChips } from "./PresenceChips";
export { ConnectionStatus } from "./ConnectionStatus";
export {
  PlacementBanner,
  type PlacementBannerProps,
  type PlacementRunner,
} from "./PlacementBanner";
// The AG-UI projection's inverse. Exported so a consumer can translate a stream
// it obtained some other way — and so the round-trip test can reach it.
export { fromAgui, resetAguiState, CUSTOM_PREFIX, METADATA_KEY } from "./agui";
