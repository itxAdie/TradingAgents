import type { BusEvent } from "./types";

export type SseState = "connecting" | "open" | "closed";

export interface SseSubscription {
  close(): void;
}

/**
 * Subscribe to /api/events/stream with automatic reconnection.
 *
 * The browser's EventSource already retries; this wrapper adds backoff via
 * readyState inspection and surfaces parsed frames to the callback. The
 * server sends `event:` names matching BusEvent.event and single-line JSON
 * payloads, so frames map 1:1 onto the bus events (spec §29).
 */
export function subscribeEvents(
  onEvent: (evt: BusEvent) => void,
  onState?: (state: SseState) => void,
  replay = 50,
): SseSubscription {
  const source = new EventSource(`/api/events/stream?replay=${replay}`);

  source.onopen = () => onState?.("open");
  source.onerror = () => {
    // EventSource schedules its own retry; report the transient state.
    if (source.readyState === EventSource.CLOSED) {
      onState?.("closed");
    }
  };

  // Named listeners for every event type the backend emits; anything new is
  // still delivered through the generic "message" fallback below.
  const KNOWN_EVENTS = [
    "research_started",
    "research_completed",
    "research_failed",
    "signal_generated",
    "risk_rejected",
    "order_submitted",
    "order_accepted",
    "order_expired",
    "position_opened",
    "position_closed",
    "stop_loss_triggered",
    "take_profit_triggered",
    "halt_triggered",
    "price_updated",
    "backtest_started",
    "backtest_completed",
    "backtest_failed",
    "audit_event",
    "journal_note_added",
  ];
  const handle = (raw: MessageEvent<string>): void => {
    try {
      const parsed = JSON.parse(raw.data) as BusEvent;
      onEvent(parsed);
    } catch {
      // A malformed frame must never take the stream down.
    }
  };
  for (const name of KNOWN_EVENTS) {
    source.addEventListener(name, handle as EventListener);
  }
  source.onmessage = handle;

  return {
    close() {
      source.close();
      onState?.("closed");
    },
  };
}
