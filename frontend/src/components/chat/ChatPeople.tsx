import { useEffect, useState } from "react";
import {
  addParticipant,
  getSession,
  giveKeyCopy,
  listParticipants,
  participantRecipient,
  removeParticipant,
  type Participant,
  type ParticipantRecipient,
  type SealedKeyWrapIn,
} from "@/api/chat";
import { openConversationKey, whyNot, wrapForRecipient, type ConversationKey, type SealedKey } from "@/vault/sealedChat";
import type { Unlocked } from "@/vault/operations";
import { pinPerson, trustPerson } from "@/vault/trust";
import { refuseUnconfirmed, usePersonTrust, type ConfirmPerson } from "@/vault/PersonTrustPrompt";
import { useVaultUnlock } from "@/vault/useVaultUnlock";
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
/** What you are told when your own copy of the key no longer opens. */
export const STALE_OWN_COPY =
  "Your copy of this conversation's key was made for a vault key you have since replaced, so you cannot give "
  + "it to anyone. Ask someone else in it to give you a fresh copy (People → Give key), then try again.";

/**
 * The conversation key, wrapped in this browser to `email`'s vault key — but only once
 * that key is one this browser trusts for them (`trust.trustPerson`: pinned before, or
 * its fingerprint accepted now through `confirm`). The server names the key; a key it
 * substituted would hand the conversation to whoever holds it, so it is never taken on
 * its word.
 */
async function wrapTo(sessionId: string, email: string, unlock: () => Promise<Unlocked>, api: PeopleApi,
                      confirm: ConfirmPerson, sealedKey: SealedKey):
    Promise<{ recipient: ParticipantRecipient; wrap: SealedKeyWrapIn; acceptedJkt: string | null }> {
  if (!sealedKey.wrap) throw new Error("You hold no key to this conversation, so you cannot give it to anyone.");
  if (whyNot(sealedKey) === "stale-wrap") throw new Error(STALE_OWN_COPY);
  const recipient = await api.participantRecipient(sessionId, email);
  const verdict = await trustPerson(recipient);
  if (verdict.status === "bad") {
    throw new Error(`The vault key canopy named for ${email} does not match its fingerprint — nothing was shared.`);
  }
  let acceptedJkt: string | null = null;
  if (verdict.status === "new" || verdict.status === "changed") {
    if (!(await confirm({ email: recipient.email || email, verdict }))) throw new Error("Not added — nothing was shared.");
    acceptedJkt = verdict.jkt;
  }
  const me = await unlock();
  let key: ConversationKey;
  try {
    key = await openConversationKey(sealedKey, me);
  } catch {
    throw new Error(STALE_OWN_COPY);
  }
  return { recipient, wrap: await wrapForRecipient(key, recipient), acceptedJkt };
}

/**
 * Add someone to a chat. In a SENSITIVE conversation the server cannot give them its
 * key (it never holds it): this browser unlocks your vault key, opens your copy of the
 * conversation key, and wraps it to their vault key — once that key is one this browser
 * trusts for them — which is what the server stores.
 */
export async function addPerson(sessionId: string, email: string, role: "editor" | "viewer",
                                unlock: () => Promise<Unlocked>, api: PeopleApi = APP_PEOPLE_API,
                                confirm: ConfirmPerson = refuseUnconfirmed): Promise<Participant[]> {
  const session = await api.getSession(sessionId);
  const sealedKey = (session.sealed_key ?? null) as SealedKey | null;
  if (!session.sensitive || !sealedKey) return api.addParticipant(sessionId, email, role);
  const { recipient, wrap, acceptedJkt } = await wrapTo(sessionId, email, unlock, api, confirm, sealedKey);
  const out = await api.addParticipant(sessionId, email, role, wrap);
  if (acceptedJkt) pinPerson(recipient.user_id, acceptedJkt, recipient.email);
  return out;
}

/** Give someone already in a private conversation their copy of its key
 *  (`POST /{id}/key-copies`): given it before it became private, or their vault key
 *  changed. The same trust rule as adding them. */
export async function giveKey(sessionId: string, person: { user_id: number; email: string },
                              unlock: () => Promise<Unlocked>, api: PeopleApi = APP_PEOPLE_API,
                              confirm: ConfirmPerson = refuseUnconfirmed): Promise<Participant[]> {
  const session = await api.getSession(sessionId);
  const sealedKey = (session.sealed_key ?? null) as SealedKey | null;
  if (!session.sensitive || !sealedKey) throw new Error("This conversation has no key to give.");
  const { recipient, wrap, acceptedJkt } = await wrapTo(sessionId, person.email, unlock, api, confirm, sealedKey);
  if (recipient.user_id !== person.user_id) throw new Error("That is not the person you asked to give it to.");
  const out = await api.giveKeyCopy(sessionId, person.user_id, wrap);
  if (acceptedJkt) pinPerson(recipient.user_id, acceptedJkt, recipient.email);
  return out;
}

/** The routes `addPerson` and `giveKey` call, through whichever credentials the surface
 *  has: the app's session cookie here, the embedded panel's own token there. */
export interface PeopleApi {
  getSession: (id: string) => Promise<{ sensitive?: boolean | null; sealed_key?: unknown }>;
  participantRecipient: (id: string, email: string) => Promise<ParticipantRecipient>;
  addParticipant: (id: string, email: string, role: "editor" | "viewer",
                   wrap?: SealedKeyWrapIn) => Promise<Participant[]>;
  giveKeyCopy: (id: string, userId: number, wrap: SealedKeyWrapIn) => Promise<Participant[]>;
}

const APP_PEOPLE_API: PeopleApi = { getSession, participantRecipient, addParticipant, giveKeyCopy };

export function ChatPeoplePanel({ sessionId, myRole }: { sessionId: string; myRole: string | null }) {
  const vault = useVaultUnlock();
  const trust = usePersonTrust();
  const [giving, setGiving] = useState<number | null>(null);
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
      why: [
        p.needs_key
          ? (isChatOwner ? "owns the chat · needs a fresh copy of its key" : "needs the key to read it")
          : isChatOwner ? "owns the chat" : null,
        p.test_key ? "agent test key" : null,
      ].filter(Boolean).join(" · ") || null,
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
      {(people ?? []).some((p) => p.needs_key) && (
        <div className="space-y-1" data-testid="needs-key">
          <p className="text-muted-foreground">
            This conversation is private: its words are sealed to the people holding its key. These people are in it
            but hold no key for their current vault key, so they cannot read it until someone in it gives them one.
          </p>
          {(people ?? []).filter((p) => p.needs_key).map((p) => (
            <div key={p.user_id} className="flex flex-wrap items-center gap-2">
              <span className="text-foreground">{p.display_name || p.email}</span>
              <button type="button" disabled={giving !== null} data-testid={`give-key-${p.user_id}`}
                className="rounded-md border border-input px-2 py-0.5 text-foreground disabled:opacity-50"
                onClick={() => {
                  setGiving(p.user_id);
                  setError("");
                  giveKey(sessionId, p, vault.unlock, APP_PEOPLE_API, trust.confirm)
                    .then(setPeople)
                    .catch((e) => setError(e instanceof Error ? e.message : "Could not give them the key."))
                    .finally(() => setGiving(null));
                }}>
                {giving === p.user_id ? "Giving…" : "Give key"}
              </button>
            </div>
          ))}
        </div>
      )}
      {isOwner ? (
        <div className="space-y-1">
          <AddPersonForm
            autoFocus
            options={SHARE_ROLES}
            defaultRole="editor"
            onAdd={async (email, role) =>
              setPeople(await addPerson(sessionId, email, role as "editor" | "viewer", vault.unlock,
                                        APP_PEOPLE_API, trust.confirm))
            }
          />
          <p className="text-muted-foreground">They must already be in this workspace.</p>
        </div>
      ) : (
        myRole && <p className="text-muted-foreground">Only the chat's owner can add people.</p>
      )}
      {trust.element}
      {vault.element}
      {error && <p className="text-destructive">{error}</p>}
    </div>
  );
}
