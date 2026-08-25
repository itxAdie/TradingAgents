/** TypeScript mirrors of tradingagents.api.schemas (verified against live payloads). */

export interface Envelope<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface ApiErrorBody {
  error?: { code: string; message: string; detail?: unknown };
  detail?: string | { msg: string }[];
}

// -- markets ----------------------------------------------------------------

export interface AssetSummary {
  asset_id: string;
  display_name: string;
  asset_class: string;
  quote_currency: string;
}

export interface QuoteOut {
  asset_id: string;
  timestamp: string;
  last: number | null;
  bid: number | null;
  ask: number | null;
  source: string;
  data_status: string;
}

export interface MarketOverviewItem {
  spec: AssetSummary;
  quote: QuoteOut | null;
  change_abs: number | null;
  change_pct: number | null;
  change_timeframe: string | null;
  freshness: "fresh" | "stale" | "unknown";
  note: string;
}

export interface ScheduleSlotView {
  asset_id: string;
  timeframe: string;
  enabled: boolean;
  next_run_at: string | null;
  last_processed_bar_close: string | null;
}

export interface ResearchRunRef {
  run_id: string;
  generated_at: string;
  signal_action: string | null;
  confidence: number | null;
}

export interface SignalRef {
  signal_id: string;
  state: string;
  action: string;
  confidence: number;
  generated_at: string;
}

export interface MarketDetail extends MarketOverviewItem {
  latest_research_run: ResearchRunRef | null;
  latest_signal_ref: SignalRef | null;
  scheduled_slots: ScheduleSlotView[];
}

export interface Candle {
  t: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
}

export interface CandlesResponse {
  asset_id: string;
  timeframe: string;
  source: string;
  data_status: string;
  freshness: "fresh" | "stale" | "unknown";
  bars: Candle[];
}

export interface IndicatorSeriesOut {
  name: string;
  values: (number | null)[];
}

export interface IndicatorsResponse {
  asset_id: string;
  timeframe: string;
  timestamps: string[];
  series: IndicatorSeriesOut[];
  na_reasons: Record<string, string>;
}

// -- research -----------------------------------------------------------------

export interface ResearchRunSummary {
  run_id: string;
  asset_id: string;
  timeframe: string;
  generated_at: string;
  signal_action: string | null;
  confidence: number | null;
  no_signal_reason: string;
  models_used: string[];
  market_data_timestamp: string | null;
}

/** Full artifact payload (report.json + optional signal.json), backend-shaped. */
export interface ResearchRunFull {
  run_id: string;
  report: Record<string, unknown>;
  signal_summary: Record<string, unknown> | null;
}

// -- signals --------------------------------------------------------------------

export interface SignalListItem {
  signal_id: string;
  asset_id: string;
  timeframe: string;
  state: string;
  action: string;
  confidence: number;
  generated_at: string;
  updated_at: string;
  rejection_reason: string;
  executed: boolean;
  risk_decision: string;
}

export interface SignalTransitionRow {
  signal_id: string;
  from_state: string;
  to_state: string;
  reason: string;
  ts: string | null;
}

export interface PaperSignalRecord {
  signal_id: string;
  account_id: string;
  environment: string;
  asset_id: string;
  timeframe: string;
  state: string;
  decision_bar_close: string;
  generated_at: string;
  market_data_timestamp: string | null;
  action: string;
  confidence: number;
  thesis: string;
  supporting_factors: string[];
  opposing_factors: string[];
  invalidation_conditions: string[];
  entry_reference: number | null;
  stop_loss_reference: number | null;
  take_profit_reference: number | null;
  data_sources: { name?: string; kind?: string }[];
  models_used: string[];
  research: {
    thesis?: string;
    bull_case?: string;
    bear_case?: string;
    invalidation_conditions?: string[];
    models_used?: string[];
    [k: string]: unknown;
  };
  rejection_reason: string;
  updated_at: string;
}

export interface PaperOrderView {
  [k: string]: unknown;
}

export interface SignalDetailResponse {
  record: PaperSignalRecord;
  transitions: SignalTransitionRow[];
  orders: PaperOrderView[];
  research_run: ResearchRunRef | null;
}

// -- portfolio -------------------------------------------------------------------

export interface PerformanceStats {
  initial_capital: number;
  final_equity: number;
  total_return_pct: number;
  win_rate?: number | null;
  n_trades_closed?: number;
  [k: string]: unknown;
}

export interface AccountReport {
  schema_name: "PAPER_ACCOUNT_REPORT";
  account_id: string;
  environment: string;
  generated_at: string;
  halted: boolean;
  halt_reason: string;
  initial_capital: number;
  cash: number;
  equity: number;
  realized_pnl: number;
  unrealized_pnl: number;
  total_costs_paid: number;
  total_return_pct: number;
  open_positions: unknown[];
  stats: PerformanceStats;
  disclaimer: string;
  orders_total: number;
}

export interface PositionRow {
  position_id: string;
  account_id: string;
  signal_id: string;
  asset_id: string;
  timeframe: string;
  direction: number;
  quantity: number;
  entry_price: number;
  raw_entry_price: number;
  entry_time: string;
  updated_at: string;
  stop_loss: number | null;
  take_profit: number | null;
  current_price: number | null;
  unrealized_pnl: number | null;
  strategy_id: string;
}

export interface EquityPoint {
  timestamp: string;
  equity: number;
  cash: number;
  exposure: number;
  open_positions: number;
  drawdown_pct: number;
}

// -- trades ------------------------------------------------------------------------

export interface TradeListItem {
  trade_id: string;
  run_id: string;
  asset_id: string;
  timeframe: string;
  direction: number;
  entry_timestamp: string;
  exit_timestamp: string;
  entry_price: number;
  exit_price: number;
  net_pnl: number;
  return_pct: number;
  holding_period: string;
  bars_held: number;
  outcome: "win" | "loss";
  exit_reason: string;
  has_journal: boolean;
  strategy_version: string;
}

export interface TimelineStage {
  stage: string;
  label: string;
  timestamp: string | null;
  detail: string;
}

export interface TradeDetailResponse {
  trade: Record<string, unknown>;
  journal: Record<string, unknown> | null;
  timeline: TimelineStage[];
  related_signal: PaperSignalRecord | null;
}

// -- risk -----------------------------------------------------------------------------

export interface RiskLimitValue {
  key: string;
  label: string;
  limit_value: number;
  current_value: number;
  utilization_pct: number;
  unit: "pct" | "count" | "currency";
}

export interface RiskStatus {
  environment: string;
  account_id: string;
  halted: boolean;
  halt_reason: string;
  equity: number;
  day_start_equity: number | null;
  peak_equity: number | null;
  gross_exposure: number;
  open_positions: number;
  limits: RiskLimitValue[];
}

export interface RiskEventItem {
  ts: string | null;
  type: string;
  asset_id: string;
  message: string;
  ref_id: string;
}

// -- backtests --------------------------------------------------------------------------

export interface BacktestStartParams {
  asset_id: string;
  timeframe: string;
  start: string;
  end: string;
  include_walk_forward: boolean;
  [k: string]: unknown;
}

export type BacktestStatus = "queued" | "running" | "completed" | "failed";

export interface BacktestJob {
  run_id: string;
  status: BacktestStatus;
  submitted_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string;
  params: BacktestStartParams;
  report_path: string | null;
}

export interface StrategyStats {
  initial_capital: number;
  final_equity: number;
  total_return_pct: number;
  annualized_return_pct: number | null;
  n_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate_pct: number | null;
  net_profit: number;
  profit_factor: number | null;
  max_drawdown_pct: number;
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
}

export interface EquityCurvePoint {
  timestamp: string;
  equity: number;
  cash: number;
  exposure: number;
  open_positions: number;
  drawdown_pct: number;
}

export interface StrategyResult {
  strategy_id: string;
  strategy_kind: string;
  params: Record<string, unknown>;
  stats: StrategyStats;
  equity_curve: EquityCurvePoint[];
  trade_count: number;
}

export interface WalkForwardWindowMetrics {
  window_id: number;
  strategy_id: string;
  train_period: [string, string] | null;
  validation_period: [string, string] | null;
  test_period: [string, string] | null;
  trades: number;
  total_return_pct: number;
  max_drawdown_pct: number;
  win_rate_pct: number | null;
  profit_factor: number | null;
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
  skipped_reason: string | null;
}

export interface WalkForwardAggregateStats {
  n_windows: number;
  profitable_windows: number;
  losing_windows: number;
  pct_profitable: number;
  average_window_return_pct: number;
  median_window_return_pct: number;
  best_window_return_pct: number;
  worst_window_return_pct: number;
  aggregate_return_pct: number | null;
  aggregate_max_drawdown_pct: number | null;
  consistency_note?: string;
  overfitting_diagnostics?: Record<string, unknown>;
}

export interface WalkForwardStrategyResult {
  strategy_id: string;
  aggregate: WalkForwardAggregateStats;
  windows: WalkForwardWindowMetrics[];
}

export interface BacktestReport {
  schema_name: "BACKTEST_REPORT";
  run_id: string;
  strategies: StrategyResult[];
  walk_forward: WalkForwardStrategyResult[];
  [key: string]: unknown;
}

// -- system / events ----------------------------------------------------------------------

export interface SystemStatusItem {
  component: string;
  status: "online" | "offline" | "degraded" | "enabled" | "disabled" | "idle";
  detail: string;
}

export interface SystemStatus {
  overall: "online" | "degraded" | "offline";
  components: SystemStatusItem[];
  generated_at: string;
}

export interface SettingsView {
  environment: string;
  account_id: string;
  trading_enabled: boolean;
  assets: string[];
  timeframes: string[];
  risk_limits: Record<string, number>;
  execution: Record<string, number>;
  enable_research_loop?: boolean;
  quote_poll_seconds?: number | null;
  secrets_exposed?: false;
}

export interface AuditEventRow {
  ts: string;
  action: string;
  detail: Record<string, unknown>;
}

export interface BusEvent {
  event: string;
  ts: string;
  payload: Record<string, unknown>;
}

// -- broker / execution (sandbox only) -------------------------------------------------

export interface BrokerAdapterInfo {
  name: string;
  sandbox: boolean;
}

export interface BrokerReconciliationMismatch {
  kind: string;
  detail: string;
  local_value: string | null;
  broker_value: string | null;
}

export interface BrokerReconciliationReport {
  ts: string;
  trigger: string;
  orders_checked: number;
  positions_checked: number;
  clean: boolean;
  mismatches: BrokerReconciliationMismatch[];
  resolutions: string[];
}

export interface BrokerStatus {
  environment: string;
  broker: string;
  account_id: string;
  account_verified: boolean;
  connection: string;
  ready: boolean;
  halted: boolean;
  halt_reason: string;
  circuit_breaker: boolean;
  circuit_breaker_reason: string;
  configuration_version: string;
  live_armed: boolean;
  last_reconciliation: BrokerReconciliationReport | null;
}

export interface BrokerStartupResult {
  ready: boolean;
  blockers: string[];
}

export interface BrokerShutdownSummary {
  open_orders_left: number | null;
  final_reconciliation_clean: boolean | null;
  final_reconciliation_error: string | null;
}
