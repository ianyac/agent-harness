import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { useInspector } from "./useInspector";

type MemoryStorage = Pick<Storage, "getItem" | "setItem"> & { values: Map<string, string> };

function storage(entries: Record<string, string> = {}): MemoryStorage {
  const values = new Map(Object.entries(entries));
  return {
    values,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, value); },
  };
}

function Harness({
  sessionId,
  store,
  showOverview = true,
}: {
  sessionId: string;
  store: MemoryStorage;
  showOverview?: boolean;
}) {
  const model = useInspector({ sessionId, storage: store });
  return (
    <>
      <output aria-label="Inspector state">
        {`${model.open ? "open" : "closed"}|${model.pinned ? "pinned" : "unpinned"}|${model.selectedActivityId ?? "overview"}|${model.width}`}
      </output>
      {showOverview ? (
        <button type="button" onClick={(event) => model.openOverview(event.currentTarget)}>Overview</button>
      ) : null}
      <button type="button" onClick={(event) => model.openActivity("activity-7", event.currentTarget)}>Activity 7</button>
      <button type="button" onClick={() => model.setPinned(!model.pinned)}>Pin</button>
      <button type="button" onClick={model.close}>Close</button>
      <button type="button" onClick={() => model.setWidth(999)}>Too wide</button>
      <button type="button" onClick={() => model.resizeBy(-999)}>Too narrow</button>
    </>
  );
}

afterEach(cleanup);

describe("useInspector", () => {
  it("starts closed and handles only the exact non-repeating Command+Shift+I chord", () => {
    const store = storage();
    render(<Harness sessionId="session-a" store={store} />);
    const state = screen.getByRole("status", { name: "Inspector state" });
    expect(state).toHaveTextContent("closed|unpinned|overview|420");

    fireEvent.keyDown(window, { key: "i", metaKey: true });
    fireEvent.keyDown(window, { key: "i", metaKey: true, shiftKey: true, ctrlKey: true });
    fireEvent.keyDown(window, { key: "i", metaKey: true, shiftKey: true, repeat: true });
    expect(state).toHaveTextContent("closed");

    fireEvent.keyDown(window, { key: "I", metaKey: true, shiftKey: true });
    expect(state).toHaveTextContent("open|unpinned|overview");
    fireEvent.keyDown(window, { key: "i", metaKey: true, shiftKey: true });
    expect(state).toHaveTextContent("closed|unpinned|overview");
  });

  it("clamps every width path and rejects malformed persisted width", async () => {
    const user = userEvent.setup();
    const store = storage({ "agent-harness:inspector-width": "not-a-width" });
    render(<Harness sessionId="session-a" store={store} />);
    const state = screen.getByRole("status", { name: "Inspector state" });
    expect(state).toHaveTextContent("|420");
    await user.click(screen.getByRole("button", { name: "Too wide" }));
    expect(state).toHaveTextContent("|640");
    await user.click(screen.getByRole("button", { name: "Too narrow" }));
    expect(state).toHaveTextContent("|320");
    expect(store.values.get("agent-harness:inspector-width")).toBe("320");
  });

  it("remembers open only for pinned stable sessions and explicit Close unpins", async () => {
    const user = userEvent.setup();
    const store = storage();
    const { rerender } = render(<Harness sessionId="session-a" store={store} />);
    await user.click(screen.getByRole("button", { name: "Activity 7" }));
    await user.click(screen.getByRole("button", { name: "Pin" }));
    expect(screen.getByRole("status", { name: "Inspector state" })).toHaveTextContent(
      "open|pinned|activity-7",
    );

    rerender(<Harness sessionId="session-b" store={store} />);
    expect(screen.getByRole("status", { name: "Inspector state" })).toHaveTextContent(
      "closed|unpinned|overview",
    );
    rerender(<Harness sessionId="session-a" store={store} />);
    expect(screen.getByRole("status", { name: "Inspector state" })).toHaveTextContent(
      "open|pinned|activity-7",
    );

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByRole("status", { name: "Inspector state" })).toHaveTextContent(
      "closed|unpinned",
    );
    expect(store.values.get("agent-harness:inspector-pinned:session-a")).toBe("false");
  });

  it("restores only a connected opening origin", async () => {
    const user = userEvent.setup();
    const store = storage();
    const { rerender } = render(<Harness sessionId="session-a" store={store} />);
    const origin = screen.getByRole("button", { name: "Overview" });
    await user.click(origin);
    screen.getByRole("button", { name: "Close" }).focus();
    await user.click(screen.getByRole("button", { name: "Close" }));
    act(() => { screen.getByRole("status", { name: "Inspector state" }); });
    expect(origin).toHaveFocus();

    await user.click(origin);
    rerender(<Harness sessionId="session-a" store={store} showOverview={false} />);
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(document.activeElement).not.toBe(origin);
  });
});
