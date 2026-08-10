import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MarkdownContent } from "./MarkdownContent";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("MarkdownContent", () => {
  it("renders inline code without leaking non-DOM props", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<MarkdownContent content={"Inline `run_turn` code"} />);

    expect(screen.getByText("run_turn")).toBeVisible();
    expect(consoleError).not.toHaveBeenCalled();
  });

  it("keeps http(s) and same-page anchor links as real anchors", () => {
    render(<MarkdownContent content={"[docs](https://example.com) and [top](#top)"} />);

    const external = screen.getByRole("link", { name: "docs" });
    expect(external).toHaveAttribute("href", "https://example.com");
    expect(external).toHaveAttribute("target", "_blank");
    const anchor = screen.getByRole("link", { name: "top" });
    expect(anchor).toHaveAttribute("href", "#top");
    expect(anchor).not.toHaveAttribute("target");
  });

  it("renders relative hrefs as copyable text instead of navigating anchors", () => {
    render(<MarkdownContent content={"[broken](foo/bar)"} copyText={() => {}} />);

    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("foo/bar")).toBeVisible();
    expect(screen.getByRole("button", { name: "Copy path" })).toBeVisible();
  });

  it("renders local paths as copyable text", () => {
    render(<MarkdownContent content={"[log](/tmp/run.log)"} copyText={() => {}} />);

    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("/tmp/run.log")).toBeVisible();
  });

  it("wraps GFM tables in a horizontal scroll container", () => {
    render(<MarkdownContent content={"| a | b |\n| --- | --- |\n| 1 | 2 |"} />);

    const table = screen.getByRole("table");
    expect(table.parentElement).toHaveClass(/tableScroll/);
  });

  it("renders blockquotes as blockquote elements", () => {
    render(<MarkdownContent content={"> quoted line"} />);

    expect(screen.getByText("quoted line").closest("blockquote")).not.toBeNull();
  });
});
