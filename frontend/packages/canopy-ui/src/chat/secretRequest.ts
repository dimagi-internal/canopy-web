import { createContext, useContext, type ReactNode } from "react";

import type { MessageSecretRequestRef } from "./protocol";

/**
 * How a host page shows an agent's request for one of its secrets.
 * The server marks the tool
 * result row (`secret_request`); the HOST renders the card, because sealing
 * needs the host's own vault code (canopy-web's `SecretSealCard`) — canopy-ui
 * carries no crypto. Absent (ace-web, the embed), the row is an ordinary tool row.
 */
export interface SecretRequestHost {
  render(ref: MessageSecretRequestRef): ReactNode;
}

export const SecretRequestHostContext = createContext<SecretRequestHost | null>(null);

export function useSecretRequestHost(): SecretRequestHost | null {
  return useContext(SecretRequestHostContext);
}
