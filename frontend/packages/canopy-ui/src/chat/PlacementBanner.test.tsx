// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { PlacementBanner, type PlacementRunner } from "./PlacementBanner";

function runner(id: string, overrides: Partial<PlacementRunner> = {}): PlacementRunner {
  return { id, name: `Runner ${id}`, online: true, ...overrides };
}

afterEach(() => {
  cleanup();
});

describe("PlacementBanner", () => {
  it("renders the bound-but-offline runner's name", () => {
    render(
      <PlacementBanner
        runnerName="Laptop"
        eligibleRunners={[]}
        onWait={vi.fn()}
        onPlace={vi.fn()}
      />,
    );

    expect(screen.getByText("Laptop is unavailable")).toBeTruthy();
  });

  it("fires onPlace with the picked runner id", () => {
    const onPlace = vi.fn();
    render(
      <PlacementBanner
        runnerName="Laptop"
        eligibleRunners={[runner("r1", { name: "Cloud" }), runner("r2", { name: "Backup" })]}
        onWait={vi.fn()}
        onPlace={onPlace}
      />,
    );

    // The picker is collapsed until "Continue on…" is clicked.
    expect(screen.queryByLabelText("Continue on")).toBeNull();
    fireEvent.click(screen.getByText("Continue on…"));

    const select = screen.getByLabelText("Continue on") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "r2" } });

    expect(onPlace).toHaveBeenCalledWith("r2");
    expect(onPlace).toHaveBeenCalledTimes(1);
  });

  it("disables both actions when busy", () => {
    render(
      <PlacementBanner
        runnerName="Laptop"
        eligibleRunners={[runner("r1")]}
        busy
        onWait={vi.fn()}
        onPlace={vi.fn()}
      />,
    );

    const waitButton = screen.getByText("Wait for it") as HTMLButtonElement;
    const continueButton = screen.getByText("Continue on…") as HTMLButtonElement;
    expect(waitButton.disabled).toBe(true);
    expect(continueButton.disabled).toBe(true);
  });

  it("fires onWait when 'Wait for it' is clicked", () => {
    const onWait = vi.fn();
    render(
      <PlacementBanner
        runnerName="Laptop"
        eligibleRunners={[]}
        onWait={onWait}
        onPlace={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByText("Wait for it"));
    expect(onWait).toHaveBeenCalledTimes(1);
  });

  it("renders an error message when provided", () => {
    render(
      <PlacementBanner
        runnerName="Laptop"
        eligibleRunners={[]}
        error="Could not place the turn."
        onWait={vi.fn()}
        onPlace={vi.fn()}
      />,
    );

    expect(screen.getByText("Could not place the turn.")).toBeTruthy();
  });

  it("names a pause as a pause, with the reason it was given", () => {
    render(
      <PlacementBanner
        runnerName="Laptop"
        eligibleRunners={[]}
        paused
        pausedNote="token limit on this account"
        onWait={vi.fn()}
        onPlace={vi.fn()}
      />,
    );

    expect(screen.getByText("Laptop is paused")).toBeTruthy();
    // The note is what tells you whether resuming is actually safe.
    expect(screen.getByText("— token limit on this account")).toBeTruthy();
  });

  it("offers Resume when the viewer can un-park the runner", () => {
    const onResume = vi.fn();
    render(
      <PlacementBanner
        runnerName="Laptop"
        eligibleRunners={[]}
        paused
        onResume={onResume}
        onWait={vi.fn()}
        onPlace={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByText("Resume"));
    expect(onResume).toHaveBeenCalledTimes(1);
  });

  it("omits Resume when no handler is given — only the pairer may resume", () => {
    render(
      <PlacementBanner
        runnerName="Laptop"
        eligibleRunners={[]}
        paused
        onWait={vi.fn()}
        onPlace={vi.fn()}
      />,
    );

    // A button that 404s reads as the runner refusing to come back.
    expect(screen.queryByText("Resume")).toBeNull();
  });

  it("keeps waiting reachable but demoted below the acting exits", () => {
    // Waiting leaves the message QUEUED until the box returns — real for a
    // reboot, wrong as a default. It stays a button (role, disabled state,
    // accessible name) but is styled as a link rather than a peer action.
    render(
      <PlacementBanner
        runnerName="Laptop"
        eligibleRunners={[]}
        paused
        onResume={vi.fn()}
        onWait={vi.fn()}
        onPlace={vi.fn()}
      />,
    );

    const wait = screen.getByText("Wait for it") as HTMLButtonElement;
    expect(wait.tagName).toBe("BUTTON");
    expect(wait.className).toContain("underline");
    expect(wait.className).not.toContain("border");
    // Resume comes first in reading order — the fix before the deferral.
    expect(
      screen.getByText("Resume").compareDocumentPosition(wait) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("renders error and info distinctly styled when both are present", () => {
    render(
      <PlacementBanner
        runnerName="Laptop"
        eligibleRunners={[]}
        error="boom"
        info="Placed."
        onWait={vi.fn()}
        onPlace={vi.fn()}
      />,
    );

    expect(screen.getByText("Placed.").className).toContain("text-muted-foreground");
    expect(screen.getByText("boom").className).toContain("text-destructive");
  });
});
