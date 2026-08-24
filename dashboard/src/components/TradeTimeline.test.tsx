import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { TimelineStage } from "../api/types";
import { TradeTimeline } from "./TradeTimeline";

const steps: TimelineStage[] = [
  {
    stage: "signal",
    label: "Signal",
    timestamp: "2026-08-21T09:00:00Z",
    detail: "BUY confidence 72%",
  },
  {
    stage: "market_data",
    label: "Market Data",
    timestamp: "2026-08-21T08:59:59Z",
    detail: "XAUUSD 1h decision bar",
  },
  {
    stage: "exit",
    label: "Exit",
    timestamp: "2026-08-21T15:00:00Z",
    detail: "take_profit → net +50.00",
  },
];

describe("TradeTimeline", () => {
  it("renders stages in canonical engine order regardless of input order", () => {
    render(<TradeTimeline steps={steps} />);
    // Only reached stages carry their backend label; assert their relative order.
    const items = screen.getAllByText(/Market Data|Signal|Exit/);
    expect(items.map((el) => el.textContent)).toEqual(["Market Data", "Signal", "Exit"]);
  });

  it("marks unreached stages as pending", () => {
    render(<TradeTimeline steps={[steps[0]]} />);
    expect(screen.getAllByText("pending").length).toBe(7);
  });

  it("shows backend detail strings verbatim", () => {
    render(<TradeTimeline steps={steps} />);
    expect(screen.getByText("take_profit → net +50.00")).toBeInTheDocument();
    expect(screen.getByText("2026-08-21 09:00:00 UTC")).toBeInTheDocument();
  });
});
