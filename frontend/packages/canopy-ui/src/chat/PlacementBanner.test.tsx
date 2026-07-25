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
});
