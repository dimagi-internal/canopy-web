import { createContext, useContext, type ReactNode } from "react";

import type { Message } from "./protocol";

/**
 * How a host page shows the files sent with a person's line. A private (sensitive)
 * conversation's files are sealed to its people, and opening one needs the host's own
 * vault code (canopy-web's `SealedAttachments`) — canopy-ui carries no crypto, so the
 * HOST renders them under the line, by its `turn_index`. Absent (ace-web, the embed),
 * a line shows only its words. `render` returns null when the line has no files.
 */
export interface AttachmentHost {
  render(message: Message): ReactNode;
}

export const AttachmentHostContext = createContext<AttachmentHost | null>(null);

export function useAttachmentHost(): AttachmentHost | null {
  return useContext(AttachmentHostContext);
}
