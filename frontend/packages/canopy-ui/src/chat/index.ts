// canopy-ui/chat — a reusable, app-agnostic multiplayer chat kit.
//
// Speaks the canonical chat WebSocket protocol (ace-web's contract; string
// ids). App specifics — the ws URL builder, the markdown renderer, session
// meta — are injected. The presentational tree is props-in / callbacks-out.

// Protocol types
export type {
  Message,
  MessageAuthor,
  MessageAppRef,
  MessageRole,
  MessageStatus,
  Draft,
  PeerDraft,
  QueuedMessage,
  Participant,
  SessionMenu,
  SessionState,
  TurnStatus,
  TypingVisibility,
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
  newClientId,
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

// Composer persistence (survives unmount / tab close)
export {
  DRAFT_STORAGE_TTL_MS,
  clearStoredDraft,
  defaultDraftStorage,
  draftStorageKey,
  readStoredDraft,
  writeStoredDraft,
  readStoredTypingVisibility,
  writeStoredTypingVisibility,
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
export { TypingRows } from "./TypingRows";
export { PeerComposers } from "./PeerComposers";
export { personColor, authorColor, initials, type PersonColor } from "./personColor";
export { QueuedRows } from "./QueuedRows";
export { isMine } from "./identity";
export { ConnectionStatus } from "./ConnectionStatus";
export {
  PlacementBanner,
  type PlacementBannerProps,
  type PlacementRunner,
} from "./PlacementBanner";
// MCP Apps (SEP-1865): render a Connected site's View for a tool result, and
// the host side of its postMessage wire (spec 2026-10-08).
export { AppView } from "./mcpApps/AppView";
export {
  AppHostContext,
  useAppHost,
  type AppHost,
  type AppRef,
  type AppReceipt,
  type AppViewResource,
} from "./mcpApps/context";
export { AppBridge, PROTOCOL_VERSION as MCP_APPS_PROTOCOL_VERSION, type AppBridgeHandlers,
         type AppBridgeOptions, type CallToolResult } from "./mcpApps/bridge";
// The AG-UI projection's inverse. Exported so a consumer can translate a stream
// it obtained some other way — and so the round-trip test can reach it.
export { fromAgui, resetAguiState, CUSTOM_PREFIX, METADATA_KEY } from "./agui";
