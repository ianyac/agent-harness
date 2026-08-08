import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./App";

describe("App", () => {
  it("renders the focused product shell", () => {
    render(<App />);

    expect(screen.getByRole("navigation", { name: "Sessions" })).toBeVisible();
    expect(screen.getByRole("main")).toBeVisible();
  });
});
