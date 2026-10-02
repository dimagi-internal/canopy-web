import { useEffect, useState } from "react";
import {
  addParticipant,
  listParticipants,
  removeParticipant,
  type Participant,
} from "@/api/chat";
import { AddPersonForm, PeopleTable, type PersonRow } from "@/components/people/PeopleTable";
import type { RoleOption } from "@/components/people/roles";

// The roles a chat's owner may hand out. Owner is whoever started the chat and
// only ever renders fixed.
const SHARE_ROLES: RoleOption[] = [
  { value: "editor", label: "Editor" },
  { value: "viewer", label: "Viewer" },
];

/**
 * "People", opened from the chat's session menu: who has been given this chat, and (for its owner)
 * giving it to a workspace teammate or changing their role.
 *
 * Being in the workspace does NOT put you in someone else's chat. The chat
 * socket used to add any member who opened one, which is what made a private
 * conversation reachable by anyone holding its link. Sharing is explicit now,
 * and this is where it happens (apps/canopy_sessions/access.py).
 *
 * Rendered through the shared PeopleTable so a role reads and changes the same
 * way here as on workspace members, an agent's access and a runner's admins.
 * Changing a role re-POSTs the participant, which the API treats as "give them
 * this chat at this role".
 */
export function ChatPeoplePanel({ sessionId, myRole }: { sessionId: string; myRole: string | null }) {
  const [people, setPeople] = useState<Participant[] | null>(null);
  const [error, setError] = useState("");
  const isOwner = myRole === "owner";

  useEffect(() => {
    let cancelled = false;
    listParticipants(sessionId)
      .then((rows) => !cancelled && setPeople(rows))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : "Could not load people."));
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  const rows: PersonRow[] = (people ?? []).map((p) => {
    const isChatOwner = p.role === "owner";
    return {
      key: p.user_id,
      name: p.display_name,
      email: p.email,
      role: p.role,
      options: SHARE_ROLES,
      editable: isOwner && !isChatOwner,
      why: isChatOwner ? "owns the chat" : null,
      onRoleChange: async (next) => setPeople(await addParticipant(sessionId, p.email, next as "editor" | "viewer")),
      onRemove:
        isOwner && !isChatOwner ? async () => setPeople(await removeParticipant(sessionId, p.user_id)) : undefined,
    };
  });

  return (
    <div className="space-y-3 text-[13px]">
      {people === null && !error && <p className="text-muted-foreground">Loading…</p>}
      {people && people.length === 0 && (
        <p className="text-muted-foreground">Nobody has been given this chat.</p>
      )}
      {people && people.length > 0 && <PeopleTable rows={rows} actions={isOwner} />}
      {isOwner ? (
        <div className="space-y-1">
          <AddPersonForm
            autoFocus
            options={SHARE_ROLES}
            defaultRole="editor"
            onAdd={async (email, role) =>
              setPeople(await addParticipant(sessionId, email, role as "editor" | "viewer"))
            }
          />
          <p className="text-muted-foreground">They must already be in this workspace.</p>
        </div>
      ) : (
        myRole && <p className="text-muted-foreground">Only the chat's owner can add people.</p>
      )}
      {error && <p className="text-destructive">{error}</p>}
    </div>
  );
}
