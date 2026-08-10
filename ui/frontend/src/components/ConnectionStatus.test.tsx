import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConnectionStatus } from "./ConnectionStatus";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("ConnectionStatus", () => {
  it("stays quiet inline for ten seconds, then exposes one truthful recovery banner", () => {
    vi.useFakeTimers();
    render(<ConnectionStatus status="reconnecting" attemptKey="client-a:1" />);
    expect(screen.getByRole("status", { name: "Local service reconnecting" })).toHaveTextContent("Reconnecting");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    act(() => vi.advanceTimersByTime(9_999));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1));
    expect(screen.getByRole("alert")).toHaveTextContent("Still reconnecting to the local service");
  });

  it("cleans timers and does not let an old attempt expose a banner for a newer connected client", () => {
    vi.useFakeTimers();
    const { rerender } = render(<ConnectionStatus status="reconnecting" attemptKey="client-a:1" />);
    rerender(<ConnectionStatus status="connected" attemptKey="client-b:1" />);
    act(() => vi.advanceTimersByTime(20_000));
    expect(screen.getByRole("status", { name: "Local service connected" })).toHaveTextContent("Connected");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("uses text and a distinct non-color icon for checking, reconnecting, and connected states", () => {
    const { rerender } = render(<ConnectionStatus status="checking" attemptKey="a" />);
    const signatures: string[] = [];
    for (const [status, name] of [
      ["checking", "Local service checking"],
      ["reconnecting", "Local service reconnecting"],
      ["connected", "Local service connected"],
    ] as const) {
      rerender(<ConnectionStatus status={status} attemptKey={status} />);
      const region = screen.getByRole("status", { name });
      signatures.push(region.querySelector("svg")?.innerHTML ?? "");
    }
    expect(new Set(signatures)).toHaveLength(3);
  });

  it("renders honest terminal copy for an unrecoverable tab with no reconnecting affordance", () => {
    vi.useFakeTimers();
    const onRetry = vi.fn();
    render(<ConnectionStatus status="unrecoverable" attemptKey="tab:1" onRetry={onRetry} />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("This tab lost its access to the local service");
    expect(alert).toHaveTextContent(/page reload signs the tab\s+out/);
    expect(alert).toHaveTextContent("your sessions and data are safe on disk");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(alert).not.toHaveTextContent("Still reconnecting");

    act(() => vi.advanceTimersByTime(20_000));
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByRole("alert")).not.toHaveTextContent("Still reconnecting");

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("omits the secondary retry control when no retry handler is wired", () => {
    render(<ConnectionStatus status="unrecoverable" attemptKey="tab:1" />);
    expect(screen.getByRole("alert")).toHaveTextContent("This tab lost its access to the local service");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
