// @vitest-environment jsdom
import { render } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "./dropdown-menu"

/** The z-index belongs on the POSITIONER. Base UI's positioner is the placed
 *  element and forms its own stacking context, so a z-index on the popup alone
 *  let the chat's sticky `z-10` bar paint over the menu and swallow the taps
 *  meant for its items (2026-09-23). jsdom cannot compute stacking, so this
 *  pins the class that makes it right. */
describe("DropdownMenuContent", () => {
  it("raises the positioner itself, not only the popup inside it", () => {
    render(
      <DropdownMenu open>
        <DropdownMenuTrigger>open</DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuItem>Share to Slack…</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>,
    )
    const popup = document.querySelector('[data-slot="dropdown-menu-content"]')!
    expect(popup).toBeTruthy()
    expect(popup.parentElement!.className).toContain("z-50")
  })
})
