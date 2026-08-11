import axe from "axe-core";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { defaultPreferences } from "./preferences";
import { Settings } from "./Settings";

afterEach(cleanup);

describe("Settings", () => {
  it("updates future-session and live UI preferences without emitting a session event", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <Settings
        open
        onOpenChange={() => {}}
        preferences={defaultPreferences}
        onChange={onChange}
        workspace="/work/acme"
        logsSupported={false}
      />,
    );

    await user.click(screen.getByRole("radio", { name: "Dark" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Default permission mode" }), "acceptAll");
    await user.click(screen.getByRole("radio", { name: "Recoverable folding" }));
    await user.click(screen.getByRole("checkbox", { name: "Notify for permission requests" }));
    await user.click(screen.getByRole("checkbox", { name: "Collapse sidebar" }));

    expect(onChange).toHaveBeenCalledWith({ appearance: "dark" });
    expect(onChange).toHaveBeenCalledWith({ defaultMode: "acceptAll" });
    expect(onChange).toHaveBeenCalledWith({ contextMode: "folding" });
    expect(onChange).toHaveBeenCalledWith({ notifyPermissions: true });
    expect(onChange).toHaveBeenCalledWith({ sidebarCollapsed: true });
  });

  it("shows the complete shortcut and honest local-data reference without credential contents", () => {
    render(
      <Settings
        open
        onOpenChange={() => {}}
        preferences={defaultPreferences}
        onChange={() => {}}
        workspace="/work/acme"
        logsSupported={false}
      />,
    );

    const panel = screen.getByRole("region", { name: "Settings" });
    expect(panel).toHaveTextContent("⌘N");
    expect(panel).toHaveTextContent("⌘K");
    expect(panel).toHaveTextContent("⌘F");
    expect(panel).toHaveTextContent("⌘⇧I");
    expect(panel).toHaveTextContent("⌘Enter");
    expect(panel).toHaveTextContent("Escape");
    expect(panel).toHaveTextContent("~/.codex/auth.json");
    expect(panel).toHaveTextContent("/work/acme/.agent/sessions");
    expect(panel).not.toHaveTextContent(/bearer|api token/i);
    expect(screen.queryByText(/logs/i)).not.toBeInTheDocument();
  });

  it("restores focus on close and has no representative axe violations", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>Open settings</button>
          <Settings
            open={open}
            onOpenChange={setOpen}
            preferences={defaultPreferences}
            onChange={() => {}}
            workspace="/work/acme"
            logsSupported
          />
        </>
      );
    }
    const { container } = render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Open settings" });
    await user.click(trigger);
    expect(screen.getByRole("region", { name: "Settings" })).toBeVisible();
    expect((await axe.run(container)).violations).toEqual([]);
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
  });
});
