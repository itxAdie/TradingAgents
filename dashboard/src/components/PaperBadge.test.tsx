import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PaperBadge } from "./PaperBadge";

describe("PaperBadge", () => {
  it("renders PAPER TRADING for paper environments", () => {
    render(<PaperBadge environment="paper" />);
    expect(screen.getByTestId("paper-badge")).toHaveTextContent("PAPER TRADING");
  });

  it("renders LIVE TRADING in red tones for live environments", () => {
    render(<PaperBadge environment="live" />);
    const badge = screen.getByTestId("paper-badge");
    expect(badge).toHaveTextContent("LIVE TRADING");
    expect(badge.className).toContain("text-red-300");
  });

  it("defaults unknown environments to PAPER styling", () => {
    render(<PaperBadge environment="test" />);
    expect(screen.getByTestId("paper-badge").className).toContain("text-blue-300");
  });
});
