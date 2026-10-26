import { describe, expect, it } from "vitest";
import type { SecretRecipients } from "@/api/chat";
import { jkt, openSealedValue } from "@/vault/zk";
import { sealForChat, secretName } from "./ShareSecret";

const ECDH = { name: "ECDH", namedCurve: "P-256" } as const;

async function machine() {
  const pair = (await crypto.subtle.generateKey(ECDH, true, ["deriveBits"])) as CryptoKeyPair;
  const jwk = (await crypto.subtle.exportKey("jwk", pair.publicKey)) as JsonWebKey;
  const public_jwk = { x: jwk.x, y: jwk.y };
  return { priv: pair.privateKey, public_jwk, target: await jkt(public_jwk) };
}

function recipients(keys: { public_jwk: { x?: string; y?: string }; target: string }[]): SecretRecipients {
  return {
    binding_id: "session:2d1c0f2e-0000-0000-0000-000000000001",
    kv: 0,
    runner_id: "r1",
    runner_name: "jj-mbp",
    recipients: keys.map((k) => ({ kind: "device", target: k.target, label: "jj-mbp", public_jwk: k.public_jwk })),
    refusal: null,
  };
}

describe("secretName", () => {
  it("shapes a name exactly as the server does, since the seal binds it", () => {
    expect(secretName("gh token")).toBe("GH_TOKEN");
    expect(secretName(" my-token ")).toBe("MY_TOKEN");
    expect(secretName("9lives")).toBe("");
    expect(secretName("")).toBe("");
  });
});

describe("sealForChat", () => {
  it("seals to the chat's machine, which alone opens it", async () => {
    const box = await machine();
    const other = await machine();
    const r = recipients([box]);
    const stored = await sealForChat(r, "GH_TOKEN", "ghp_secret");
    expect(stored.startsWith("zks1:")).toBe(true);
    expect(stored).not.toContain("ghp_secret");
    expect(await openSealedValue(stored, r.binding_id, "GH_TOKEN", "device", box.target, box.priv)).toBe("ghp_secret");
    await expect(openSealedValue(stored, r.binding_id, "GH_TOKEN", "device", box.target, other.priv)).rejects.toThrow();
  });

  it("targets the thumbprint it computes, not the one the server claims", async () => {
    const box = await machine();
    const r = recipients([{ ...box, target: "server-said-so" }]);
    const stored = await sealForChat(r, "GH_TOKEN", "v");
    expect(await openSealedValue(stored, r.binding_id, "GH_TOKEN", "device", box.target, box.priv)).toBe("v");
  });

  it("refuses with the server's sentence when nothing can open it", async () => {
    const r = { ...recipients([]), refusal: "This chat is not running on a machine yet." };
    await expect(sealForChat(r, "GH_TOKEN", "v")).rejects.toThrow("not running on a machine yet");
  });
});
