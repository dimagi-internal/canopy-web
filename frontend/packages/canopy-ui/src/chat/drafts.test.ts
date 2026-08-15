import { afterEach, describe, expect, it, vi } from "vitest"

import type { Draft } from "./protocol"
import {
  DRAFT_STORAGE_TTL_MS,
  IDLE_THRESHOLD_MS,
  clearStoredDraft,
  draftStorageKey,
  isDraftIdle,
  msUntilDraftIdle,
  readStoredDraft,
  shouldSyncDraftLive,
  writeStoredDraft,
} from "./drafts"

const NOW = 1_700_000_000_000

function draftEditedAt(msAgo: number): Draft {
  return {
    id: "d1",
    slot: "next",
    status: "open",
    body: "",
    version: 0,
    last_editor: 1,
    last_edit_at: new Date(NOW - msAgo).toISOString(),
  }
}

afterEach(() => {
  vi.useRealTimers()
})

describe("isDraftIdle", () => {
  it("treats a null/undefined draft as idle", () => {
    expect(isDraftIdle(null)).toBe(true)
    expect(isDraftIdle(undefined)).toBe(true)
  })

  it("treats a draft with no last_edit_at as idle", () => {
    const d = { ...draftEditedAt(0), last_edit_at: "" }
    expect(isDraftIdle(d)).toBe(true)
  })

  it("is NOT idle immediately after an edit", () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    // edited 500ms ago — well within the 2s threshold
    expect(isDraftIdle(draftEditedAt(500))).toBe(false)
  })

  it("IS idle once more than the threshold has elapsed", () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    expect(isDraftIdle(draftEditedAt(IDLE_THRESHOLD_MS + 1))).toBe(true)
  })
})

describe("msUntilDraftIdle", () => {
  it("returns 0 for a null draft", () => {
    expect(msUntilDraftIdle(null)).toBe(0)
  })

  it("returns the remaining time before the idle transition", () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    // edited 500ms ago → 1500ms remain
    expect(msUntilDraftIdle(draftEditedAt(500))).toBe(1500)
  })

  it("clamps to 0 once past the threshold", () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    expect(msUntilDraftIdle(draftEditedAt(IDLE_THRESHOLD_MS + 5000))).toBe(0)
  })
})

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
