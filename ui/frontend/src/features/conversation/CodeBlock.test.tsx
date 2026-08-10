import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { CodeBlock } from "./CodeBlock";

afterEach(cleanup);

describe("CodeBlock", () => {
  it("classifies diff lines and preserves the newline structure", () => {
    const code = "@@ -1,2 +1,2 @@\n-removed\n+added\ncontext";
    render(<CodeBlock code={code} language="diff" copyText={() => {}} />);

    const region = screen.getByRole("region", { name: "Diff" });
    const lines = [...region.querySelectorAll("[data-diff-line]")];
    expect(lines.map((line) => line.getAttribute("data-diff-line"))).toEqual([
      "hunk",
      "removal",
      "addition",
      "context",
    ]);
    expect(region.querySelector("pre")?.textContent).toBe(code);
  });
});
