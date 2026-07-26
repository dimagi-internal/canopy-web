import { afterEach, describe, expect, it, vi } from "vitest"

import type { Draft } from "./protocol"
import {
  IDLE_THRESHOLD_MS,
  isDraftIdle,
  msUntilDraftIdle,
  shouldSyncDraftLive,
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
