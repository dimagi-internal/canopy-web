import { useState } from "react";
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
import { ChatPeoplePanel } from "./ChatPeople";
import { ShareToSlackForm } from "./ShareToSlack";
import { ShareSecretForm } from "./ShareSecret";

/**
 * Everything you can DO to a chat, behind one button.
 *
 * The chat header used to carry five buttons (Every reply, People, Share to
 * Slack, Close session, Reset from transcript). On a phone they took two rows,
 * and before they wrapped they pushed the page 145px sideways. None of them is
 * something you do while reading a conversation, so they live here. The header
 * keeps what you read: the title and whether the agent is working.
 *
 * "Load earlier"/"Load full session" joined them for the same reason (Jonathan,
 * 2026-10-05): they used to be a strip PINNED above the transcript whenever the
 * server held more history than was loaded, which is most of the time on an
 * old session — a bar for a control that, once a session is a few days old,
 * you almost never tap. The warning it can also carry (history unavailable —
 * runner offline) stays inline on the transcript, because that one is a fact
 * about what you're reading, not an action to take.
 *
 * Ordered by how often each is used. Close is last and set apart, because it
 * is the one that cannot be undone (it deletes the emdash task).
 */
export function ChatSessionMenu({
  sessionId,
  myRole,
  notifyEveryCompletion,
  onToggleNotify,
  shareDisabledReason,
  onShare,
  showLoadEarlier,
  onLoadEarlier,
  loadingEarlier,
  showLoadFull,
  onLoadFull,
  loadingFull,
  onReset,
  resetting,
  onClose,
  closing,
  typingVisibility,
  onTypingVisibilityChange,
}: {
  sessionId: string;
  myRole: string | null;
  /** Undefined until the session has loaded: the toggle is hidden until then. */
  notifyEveryCompletion?: boolean;
  onToggleNotify: () => void;
  /** Why the session cannot take a message now; sharing to Slack IS a message. */
  shareDisabledReason?: string;
  onShare: (command: string) => Promise<void>;
  /** The server holds more than is loaded — see `hasMoreBefore` in ChatPage. */
  showLoadEarlier?: boolean;
  onLoadEarlier?: () => void;
  loadingEarlier?: boolean;
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
}) {
  const [panel, setPanel] = useState<"people" | "slack" | "secret" | null>(null);
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
          <DropdownMenuItem
            inset
            disabled={Boolean(shareDisabledReason)}
            onClick={() => setPanel("slack")}
          >
            Share to Slack…
          </DropdownMenuItem>
          <DropdownMenuItem
            inset
            data-testid="share-secret"
            onClick={() => setPanel("secret")}
          >
            Secrets…
          </DropdownMenuItem>
          <DropdownMenuItem inset disabled={resetting} onClick={onReset}>
            {resetting ? "Resetting…" : "Reset from transcript"}
          </DropdownMenuItem>
          {(showLoadEarlier || showLoadFull) && <DropdownMenuSeparator />}
          {showLoadEarlier && (
            <DropdownMenuItem inset disabled={loadingEarlier} onClick={onLoadEarlier} data-testid="menu-load-earlier">
              {loadingEarlier ? "Loading…" : "Load earlier messages"}
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
        <DialogContent className="w-[calc(100%-2rem)] max-w-md bg-card">
          <DialogTitle className="text-sm font-semibold text-foreground">
            {panel === "people" ? "People" : panel === "secret" ? "Secrets" : "Share to Slack"}
          </DialogTitle>
          {panel === "secret" && (
            <ShareSecretForm sessionId={sessionId} onDone={() => setPanel(null)} />
          )}
          {panel === "people" && <ChatPeoplePanel sessionId={sessionId} myRole={myRole} />}
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
