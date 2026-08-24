import { useEffect, useState } from "react";
import type {
  AccountReport,
  AuditEventRow,
  BacktestJob,
  BacktestReport,
  BusEvent,
  CandlesResponse,
  EquityPoint,
  IndicatorsResponse,
  MarketDetail,
  MarketOverviewItem,
  PositionRow,
  ResearchRunFull,
  ResearchRunSummary,
  RiskEventItem,
  RiskStatus,
  SettingsView,
  SignalDetailResponse,
  SignalListItem,
  SystemStatus,
  TradeDetailResponse,
  TradeListItem,
} from "./types";
import type { Envelope } from "./types";
import { get, getPage, send } from "./client";
import { subscribeEvents, type SseState, type SseSubscription } from "./sse";

// -- markets -------------------------------------------------------------------

export const fetchMarkets = () => get<MarketOverviewItem[]>("/markets");

export async function fetchAsset(assetId: string): Promise<MarketDetail> {
  return get<MarketDetail>(`/markets/${encodeURIComponent(assetId)}`);
}

export async function fetchCandles(
  assetId: string,
  timeframe: string,
  limit = 300,
): Promise<CandlesResponse> {
  return get<CandlesResponse>(
    `/markets/${encodeURIComponent(assetId)}/candles?timeframe=${timeframe}&limit=${limit}`,
  );
}

export async function fetchIndicators(
  assetId: string,
  timeframe: string,
  kinds: string,
  limit = 300,
): Promise<IndicatorsResponse> {
  const qs = new URLSearchParams({ timeframe, kinds, limit: String(limit) });
  return get<IndicatorsResponse>(`/markets/${encodeURIComponent(assetId)}/indicators?${qs}`);
}

// -- research ---------------------------------------------------------------------

export async function fetchResearch(params: {
  asset_id?: string;
  action?: string;
  limit?: number;
}): Promise<Envelope<ResearchRunSummary>> {
  const qs = new URLSearchParams(
    Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== "")
      .map(([k, v]) => [k, String(v)]),
  );
  return getPage<ResearchRunSummary>(`/research?${qs}`);
}

export async function fetchResearchRun(runId: string): Promise<ResearchRunFull> {
  return get<ResearchRunFull>(`/research/${encodeURIComponent(runId)}`);
}

// -- signals -------------------------------------------------------------------------

export async function fetchSignals(params: {
  state?: string;
  min_confidence?: number;
  limit?: number;
}): Promise<Envelope<SignalListItem>> {
  const qs = new URLSearchParams(
    Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== "")
      .map(([k, v]) => [k, String(v)]),
  );
  return getPage<SignalListItem>(`/signals?${qs}`);
}

export async function fetchSignal(id: string): Promise<SignalDetailResponse> {
  return get<SignalDetailResponse>(`/signals/${encodeURIComponent(id)}`);
}

// -- portfolio --------------------------------------------------------------------------

export const fetchPortfolio = () => get<AccountReport>("/portfolio");

export async function fetchEquity(limit = 500): Promise<EquityPoint[]> {
  const page = await getPage<EquityPoint>("/portfolio/equity-curve?limit=" + limit);
  return page.items;
}

export async function fetchPositions(limit = 100): Promise<PositionRow[]> {
  const page = await getPage<PositionRow>(`/portfolio/positions?limit=${limit}`);
  return page.items;
}

// -- trades ---------------------------------------------------------------------------------

export async function fetchTrades(outcome?: "win" | "loss"): Promise<TradeListItem[]> {
  const qs = outcome ? `?outcome=${outcome}` : "";
  const page = await getPage<TradeListItem>(`/trades${qs}`);
  return page.items;
}

export async function fetchTradeDetail(tradeId: string): Promise<TradeDetailResponse> {
  return get<TradeDetailResponse>(`/trades/${encodeURIComponent(tradeId)}`);
}

export async function addJournalNote(tradeId: string, author: string, text: string) {
  return send<{ status: string; trade_id: string }>(
    "PUT",
    `/trades/${encodeURIComponent(tradeId)}/journal`,
    { text, author },
  );
}

// -- risk --------------------------------------------------------------------------------------

export async function fetchRisk(): Promise<RiskStatus> {
  return get<RiskStatus>("/risk");
}

export async function fetchRiskEvents(): Promise<RiskEventItem[]> {
  const page = await getPage<RiskEventItem>("/risk/events");
  return page.items;
}

// -- backtests ------------------------------------------------------------------------------------

export interface BacktestStartPayload {
  asset_id: string;
  timeframe: string;
  start: string;
  end: string;
  include_walk_forward?: boolean;
}

export async function submitBacktest(payload: BacktestStartPayload): Promise<BacktestJob> {
  return send<BacktestJob>("POST", "/backtests", payload);
}

export async function fetchBacktest(
  runId: string,
): Promise<{ job: BacktestJob; report: BacktestReport | null }> {
  return get(`/backtests/${encodeURIComponent(runId)}`);
}

export async function fetchBacktests(): Promise<BacktestJob[]> {
  const page = await getPage<BacktestJob>("/backtests");
  return page.items;
}

// -- system / events ----------------------------------------------------------------------------------

export const fetchSystemStatus = () => get<SystemStatus>("/system/status");
export const fetchSettings = () => get<SettingsView>("/system/settings");

export async function fetchAudit(limit = 50): Promise<AuditEventRow[]> {
  const page = await getPage<AuditEventRow>(`/system/audit?limit=${limit}`);
  return page.items;
}

/** Live bus feed; subscribe once per layout mount. */
export function useEventBuffer(max = 200): {
  events: BusEvent[];
  state: SseState;
} {
  const [events, setEvents] = useState<BusEvent[]>([]);
  const [state, setState] = useState<SseState>("connecting");

  useEffect(() => {
    let sub: SseSubscription | null = null;
    sub = subscribeEvents(
      (evt) =>
        setEvents((prev) => {
          const next = [...prev, evt];
          return next.length > max ? next.slice(next.length - max) : next;
        }),
      setState,
    );
    return () => sub?.close();
  }, [max]);

  return { events, state };
}
