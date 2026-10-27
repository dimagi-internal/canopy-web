import { useEffect, useState } from "react";
import { MoreHorizontal } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "canopy-ui/ui";
import type { TypingVisibility } from "canopy-ui/chat";
import { copyText } from "canopy-ui/lib";
import { scopedPath } from "@/lib/scopedLinks";
import { ChatPeoplePanel } from "./ChatPeople";
import { ShareToSlackForm } from "./ShareToSlack";
import { ShareSecretForm } from "./ShareSecret";
import { MoveWorkspaceForm, type MoveTarget } from "./MoveWorkspaceForm";
import { MemoryDots, SessionMemoryPanel, memorySummary, type SessionMemoryControl } from "./SessionMemoryToggles";
import { SessionInfoPanel } from "./SessionInfoPanel";

/**
 * Everything you can DO to a chat, behind one button.
 *
 * The chat header used to carry five buttons (Every reply, People, Share to
 * Slack, Close session, Reset from transcript). On a phone they took two rows,
 * and before they wrapped they pushed the page 145px sideways. None of them is
 * something you do while reading a conversation, so they live here. The header
 * keeps what you read: the title and whether the agent is working.
 *
 * "Load all messages"/"Load full session" joined them for the same reason (Jonathan,
 * 2026-10-05): they used to be a strip PINNED above the transcript whenever the
 * server held more history than was loaded, which is most of the time on an
 * old session — a bar for a control that, once a session is a few days old,
 * you almost never tap. The warning it can also carry (history unavailable —
 * runner offline) stays inline on the transcript, because that one is a fact
 * about what you're reading, not an action to take.
 *
 * Your memory in this conversation joined them too (Jonathan, 2026-10-19): a
 * chip in the header gave a setting you rarely change top billing. The item
 * carries its dots (green working, red not) so the state is one tap away.
 *
 * Session info sits with People: both are about the conversation rather than
 * something you do to it — who is in it, and where it came from. "Copy join
 * link" sits with them too: the chat's workspace address, which a teammate opens
 * to join live. The link is no grant — People decides who it lets in.
 *
 * Ordered by how often each is used. Close is last and set apart, because it
 * is the one that cannot be undone (it deletes the emdash task).
 */
export function ChatSessionMenu({
  sessionId,
  agentSlug,
  myRole,
  canAddPeople = false,
  notifyEveryCompletion,
  onToggleNotify,
  shareDisabledReason,
  onShare,
  showLoadAll,
  onLoadAll,
  loadingAll,
  showLoadFull,
  onLoadFull,
  loadingFull,
  onReset,
  resetting,
  onClose,
  closing,
  typingVisibility,
  onTypingVisibilityChange,
  slackThread,
  onDetachSlack,
  detachingSlack,
  workspace,
  moveTargets,
  onMove,
  memory,
}: {
  sessionId: string;
  /** The agent this chat is with: Share secret offers its vault for a declared name. */
  agentSlug?: string | null;
  myRole: string | null;
  canAddPeople?: boolean;
  /** Undefined until the session has loaded: the toggle is hidden until then. */
  notifyEveryCompletion?: boolean;
  onToggleNotify: () => void;
  /** Why the session cannot take a message now; sharing to Slack IS a message. */
  shareDisabledReason?: string;
  onShare: (command: string) => Promise<void>;
  /** The server holds more than is loaded — see `hasMoreBefore` in ChatPage. */
  showLoadAll?: boolean;
  onLoadAll?: () => void;
  loadingAll?: boolean;
  /** See `shouldShowLoadFull` in chatPageLogic.ts. */
  showLoadFull?: boolean;
  onLoadFull?: () => void;
  loadingFull?: boolean;
  onReset: () => void;
  resetting: boolean;
  onClose: () => void;
  closing: boolean;
  /** Both undefined for a viewer (nothing to choose) — the section hides. */
  typingVisibility?: TypingVisibility;
  onTypingVisibilityChange?: (visibility: TypingVisibility) => void;
  /** The Slack thread this session is connected to, or null/undefined when none. */
  slackThread?: { channel: string; url?: string } | null;
  /** Disconnect from that thread. Omitted for someone who can't act in the session. */
  onDetachSlack?: () => void;
  detachingSlack?: boolean;
  /** The workspace the session is filed in now. */
  workspace?: string;
  /** Workspaces you could move it to. "Move to workspace…" shows only when the
   *  session is yours to move AND there is somewhere else to put it. */
  moveTargets?: MoveTarget[];
  onMove?: (workspace: string) => Promise<void>;
  /** Agent memory for this session; null/undefined hides the item. */
  memory?: SessionMemoryControl | null;
}) {
  const [panel, setPanel] = useState<"people" | "info" | "slack" | "secret" | "move" | "memory" | null>(null);
  const [copied, setCopied] = useState<"yes" | "blocked" | null>(null);
  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(null), 1500);
    return () => window.clearTimeout(t);
  }, [copied]);
  const joinUrl = workspace ? `${window.location.origin}${scopedPath(workspace, `/sessions/${sessionId}`)}` : null;
  const canMove = Boolean(onMove && workspace && moveTargets && moveTargets.some((t) => t.slug !== workspace));
  // Every item is inset to the checkbox item's gutter, so the list is one column.

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger
          data-testid="chat-session-menu"
          aria-label="Session actions"
          title="Session actions"
          className="flex min-h-11 min-w-11 items-center justify-center rounded-md border border-border bg-card text-foreground-secondary hover:bg-muted sm:min-h-8 sm:min-w-8"
        >
          <MoreHorizontal className="h-4 w-4" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-60">
          {notifyEveryCompletion !== undefined && (
            <DropdownMenuCheckboxItem
              data-testid="notify-every-completion"
              checked={notifyEveryCompletion}
              onCheckedChange={() => onToggleNotify()}
            >
              Notify on every reply
            </DropdownMenuCheckboxItem>
          )}
          {typingVisibility != null && onTypingVisibilityChange != null && (
            <>
              <DropdownMenuLabel className="pl-8 text-xs font-normal text-muted-foreground">
                Others see while you type
              </DropdownMenuLabel>
              <DropdownMenuRadioGroup
                value={typingVisibility}
                onValueChange={(value) => onTypingVisibilityChange(value as TypingVisibility)}
              >
                <DropdownMenuRadioItem value="live">My text</DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="typing">Typing…</DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="hidden">Nothing</DropdownMenuRadioItem>
              </DropdownMenuRadioGroup>
              <DropdownMenuSeparator />
            </>
          )}
          <DropdownMenuItem inset onClick={() => setPanel("people")}>People…</DropdownMenuItem>
          {joinUrl && (
            <DropdownMenuItem
              inset
              data-testid="copy-join-link"
              title="The link a teammate opens to join this chat live. It lets in only the people this chat is given to (People…)."
              // Stays open so the item can say it worked.
              closeOnClick={false}
              onClick={() => void copyText(joinUrl).then((ok) => setCopied(ok ? "yes" : "blocked"))}
            >
              {copied === "yes" ? "Join link copied" : copied === "blocked" ? "Clipboard blocked here" : "Copy join link"}
            </DropdownMenuItem>
          )}
          <DropdownMenuItem inset data-testid="session-info-item" onClick={() => setPanel("info")}>
            Session info…
          </DropdownMenuItem>
          {memory && (
            <DropdownMenuItem
              inset
              data-testid="session-memory"
              aria-label={`Memory… ${memorySummary(memory)}`}
              onClick={() => setPanel("memory")}
            >
              <span className="flex w-full items-center justify-between gap-2">
                Memory…
                <span className="text-[12px] text-muted-foreground">
                  <MemoryDots memory={memory} />
                </span>
              </span>
            </DropdownMenuItem>
          )}
          <DropdownMenuItem
            inset
            disabled={Boolean(shareDisabledReason)}
            title={shareDisabledReason || undefined}
            onClick={() => setPanel("slack")}
          >
            Share to Slack…
          </DropdownMenuItem>
          {slackThread && (
            <>
              {slackThread.url && (
                <DropdownMenuItem
                  inset
                  data-testid="slack-thread-link"
                  onClick={() => window.open(slackThread.url, "_blank", "noopener,noreferrer")}
                >
                  Open Slack thread
                </DropdownMenuItem>
              )}
              {onDetachSlack && (
                <DropdownMenuItem
                  inset
                  data-testid="detach-slack"
                  disabled={detachingSlack}
                  onClick={onDetachSlack}
                >
                  {detachingSlack ? "Disconnecting…" : "Disconnect from Slack"}
                </DropdownMenuItem>
              )}
            </>
          )}
          <DropdownMenuItem
            inset
            data-testid="share-secret"
            onClick={() => setPanel("secret")}
          >
            Secrets…
          </DropdownMenuItem>
          {canMove && (
            <DropdownMenuItem inset data-testid="move-workspace" onClick={() => setPanel("move")}>
              Move to workspace…
            </DropdownMenuItem>
          )}
          <DropdownMenuItem inset disabled={resetting} onClick={onReset}>
            {resetting ? "Resetting…" : "Reset from transcript"}
          </DropdownMenuItem>
          {(showLoadAll || showLoadFull) && <DropdownMenuSeparator />}
          {showLoadAll && (
            <DropdownMenuItem inset disabled={loadingAll} onClick={onLoadAll} data-testid="menu-load-all">
              {loadingAll ? "Loading…" : "Load all messages"}
            </DropdownMenuItem>
          )}
          {showLoadFull && (
            <DropdownMenuItem inset disabled={loadingFull} onClick={onLoadFull} data-testid="menu-load-full">
              {loadingFull ? "Loading…" : "Load full session"}
            </DropdownMenuItem>
          )}
          <DropdownMenuSeparator />
          <DropdownMenuItem
            inset
            disabled={closing}
            onClick={onClose}
            className="text-destructive data-[highlighted]:text-destructive"
          >
            {closing ? "Closing…" : "Close session"}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={panel !== null} onOpenChange={(open: boolean) => !open && setPanel(null)}>
        <DialogContent className={`w-[calc(100%-2rem)] ${panel === "info" ? "max-w-2xl" : "max-w-md"} bg-card`}>
          <DialogTitle className="text-sm font-semibold text-foreground">
            {panel === "people"
              ? "People"
              : panel === "info"
                ? "Session info"
              : panel === "memory"
                ? "Your memory in this conversation"
              : panel === "secret"
                ? "Secrets"
                : panel === "move"
                  ? "Move to workspace"
                  : "Share to Slack"}
          </DialogTitle>
          {panel === "secret" && (
            <ShareSecretForm sessionId={sessionId} agentSlug={agentSlug} onDone={() => setPanel(null)} />
          )}
          {panel === "memory" && memory && (
            <div className="text-[12px]">
              <SessionMemoryPanel memory={memory} />
            </div>
          )}
          {panel === "people" && <ChatPeoplePanel sessionId={sessionId} myRole={myRole} canAddPeople={canAddPeople} />}
          {panel === "info" && <SessionInfoPanel sessionId={sessionId} />}
          {panel === "move" && canMove && (
            <MoveWorkspaceForm
              current={workspace!}
              targets={moveTargets!}
              onMove={onMove!}
              onDone={() => setPanel(null)}
            />
          )}
          {panel === "slack" && (
            <>
              <p className="text-[12px] text-muted-foreground">
                The session summarizes what it's doing and posts it to the channel.
              </p>
              <ShareToSlackForm onShare={onShare} onDone={() => setPanel(null)} />
            </>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
