import { describe, expect, it } from "vitest"

import {
  DRAFT_STORAGE_TTL_MS,
  clearStoredDraft,
  draftStorageKey,
  isMorePrivateVisibility,
  readStoredDraft,
  readStoredTypingVisibility,
  shouldSyncDraftLive,
  writeStoredDraft,
  writeStoredTypingVisibility,
} from "./drafts"

const NOW = 1_700_000_000_000

describe("shouldSyncDraftLive", () => {
  it("does not mirror keystrokes when you are alone", () => {
    // The single-player case: a per-keystroke draft.update costs a round trip,
    // an echo that can rewind the textarea, and a version to disagree about —
    // and protects no co-editor, because there isn't one.
    expect(shouldSyncDraftLive([1])).toBe(false)
  })

  it("treats an empty presence set as alone", () => {
    // Pre-connect: presence has not landed yet, which is precisely when there
    // is nobody to sync with.
    expect(shouldSyncDraftLive([])).toBe(false)
  })

  it("mirrors keystrokes once somebody else is present", () => {
    expect(shouldSyncDraftLive([1, 2])).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Composer persistence
// ---------------------------------------------------------------------------

function fakeStorage(seed: Record<string, string> = {}) {
  const map = new Map(Object.entries(seed))
  return {
    map,
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  }
}

const KEY = "sess-1"

describe("readStoredDraft", () => {
  it("returns null when there is nothing stored", () => {
    expect(readStoredDraft(fakeStorage(), KEY)).toBeNull()
  })

  it("round-trips a written body", () => {
    const s = fakeStorage()
    writeStoredDraft(s, KEY, "half a thought", NOW)
    expect(readStoredDraft(s, KEY, NOW + 1000)).toBe("half a thought")
  })

  it("drops and prunes an entry past the TTL", () => {
    const s = fakeStorage()
    writeStoredDraft(s, KEY, "stale", NOW)
    expect(readStoredDraft(s, KEY, NOW + DRAFT_STORAGE_TTL_MS + 1)).toBeNull()
    expect(s.map.has(draftStorageKey(KEY))).toBe(false)
  })

  it("keeps an entry right up to the TTL boundary", () => {
    const s = fakeStorage()
    writeStoredDraft(s, KEY, "fresh enough", NOW)
    expect(readStoredDraft(s, KEY, NOW + DRAFT_STORAGE_TTL_MS)).toBe("fresh enough")
  })

  it("drops and prunes malformed JSON", () => {
    const s = fakeStorage({ [draftStorageKey(KEY)]: "{not json" })
    expect(readStoredDraft(s, KEY, NOW)).toBeNull()
    expect(s.map.has(draftStorageKey(KEY))).toBe(false)
  })

  it("drops an entry of the wrong shape", () => {
    const s = fakeStorage({ [draftStorageKey(KEY)]: JSON.stringify({ body: 42 }) })
    expect(readStoredDraft(s, KEY, NOW)).toBeNull()
  })

  it("reports an empty stored body as nothing to restore", () => {
    // "" must not shadow a server draft the host does want rendered.
    const s = fakeStorage({
      [draftStorageKey(KEY)]: JSON.stringify({ body: "", at: NOW }),
    })
    expect(readStoredDraft(s, KEY, NOW)).toBeNull()
  })

  it("is inert without a storage or without a key", () => {
    expect(readStoredDraft(null, KEY)).toBeNull()
    expect(readStoredDraft(fakeStorage(), "")).toBeNull()
  })

  it("survives a storage that throws on read", () => {
    // Safari private mode / blocked third-party storage.
    const s = {
      getItem: () => {
        throw new Error("SecurityError")
      },
      setItem: () => {},
      removeItem: () => {},
    }
    expect(() => readStoredDraft(s, KEY)).not.toThrow()
    expect(readStoredDraft(s, KEY)).toBeNull()
  })
})

describe("writeStoredDraft", () => {
  it("clears the entry instead of storing an empty body", () => {
    const s = fakeStorage()
    writeStoredDraft(s, KEY, "typed", NOW)
    writeStoredDraft(s, KEY, "", NOW)
    expect(s.map.has(draftStorageKey(KEY))).toBe(false)
  })

  it("keeps one entry per session", () => {
    const s = fakeStorage()
    writeStoredDraft(s, "a", "for a", NOW)
    writeStoredDraft(s, "b", "for b", NOW)
    expect(readStoredDraft(s, "a", NOW)).toBe("for a")
    expect(readStoredDraft(s, "b", NOW)).toBe("for b")
  })

  it("swallows a quota error rather than breaking a keystroke", () => {
    const s = {
      getItem: () => null,
      setItem: () => {
        throw new Error("QuotaExceededError")
      },
      removeItem: () => {},
    }
    expect(() => writeStoredDraft(s, KEY, "x")).not.toThrow()
  })
})

describe("clearStoredDraft", () => {
  it("removes a stored draft", () => {
    const s = fakeStorage()
    writeStoredDraft(s, KEY, "sent now", NOW)
    clearStoredDraft(s, KEY)
    expect(readStoredDraft(s, KEY, NOW)).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Typing-visibility persistence — per browser, not per session.
// ---------------------------------------------------------------------------

describe("readStoredTypingVisibility / writeStoredTypingVisibility", () => {
  it("defaults to null when nothing is stored", () => {
    expect(readStoredTypingVisibility(fakeStorage())).toBeNull()
  })

  it("round-trips a written mode", () => {
    const s = fakeStorage()
    writeStoredTypingVisibility(s, "typing")
    expect(readStoredTypingVisibility(s)).toBe("typing")
  })

  it("rejects a value that isn't one of the three modes", () => {
    const s = fakeStorage({ "canopy.chat.typingVisibility": "loud" })
    expect(readStoredTypingVisibility(s)).toBeNull()
  })

  it("is inert without a storage", () => {
    expect(readStoredTypingVisibility(null)).toBeNull()
    expect(() => writeStoredTypingVisibility(null, "hidden")).not.toThrow()
  })

  it("survives a storage that throws", () => {
    const hostile = {
      getItem: () => {
        throw new Error("SecurityError")
      },
      setItem: () => {
        throw new Error("SecurityError")
      },
      removeItem: () => {},
    }
    expect(readStoredTypingVisibility(hostile)).toBeNull()
    expect(() => writeStoredTypingVisibility(hostile, "live")).not.toThrow()
  })
})

describe("isMorePrivateVisibility", () => {
  it("orders live < typing < hidden", () => {
    expect(isMorePrivateVisibility("typing", "live")).toBe(true)
    expect(isMorePrivateVisibility("hidden", "live")).toBe(true)
    expect(isMorePrivateVisibility("hidden", "typing")).toBe(true)
  })

  it("is false for an equal or looser mode", () => {
    expect(isMorePrivateVisibility("live", "live")).toBe(false)
    expect(isMorePrivateVisibility("live", "hidden")).toBe(false)
    expect(isMorePrivateVisibility("typing", "hidden")).toBe(false)
  })
})
