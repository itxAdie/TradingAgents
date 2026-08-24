import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchBacktest, fetchBacktests, fetchMarkets, submitBacktest, type BacktestStartPayload } from "../api/hooks";
import { Card, Chip, ErrorNote, Loading, statusTone } from "../components/ui";
import type { StrategyResult, WalkForwardWindowMetrics } from "../api/types";
import { fmtPct, fmtTime } from "../lib/format";

export function BacktestsPage() {
  const qc = useQueryClient();
  const jobs = useQuery({ queryKey: ["backtests"], queryFn: fetchBacktests });
  const [selected, setSelected] = useState<string | null>(null);

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">Backtests</h1>
      <SubmitForm onSubmitted={(runId) => setSelected(runId)} />

      <Card title="Runs">
        {jobs.isPending ? (
          <Loading />
        ) : jobs.error ? (
          <ErrorNote error={jobs.error} />
        ) : jobs.data && jobs.data.length > 0 ? (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wider text-slate-500">
                <th className="py-2">Run</th>
                <th className="py-2">Asset</th>
                <th className="py-2">Window</th>
                <th className="py-2">WF</th>
                <th className="py-2">Status</th>
                <th className="py-2">Submitted</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {jobs.data.map((j) => (
                <tr
                  key={j.run_id}
                  onClick={() => setSelected(j.run_id)}
                  className={
                    "cursor-pointer border-t border-slate-800/60 hover:bg-slate-900/40" +
                    (selected === j.run_id ? " bg-slate-900/60" : "")
                  }
                >
                  <td className="py-2 text-xs">{j.run_id}</td>
                  <td>{j.params.asset_id}</td>
                  <td className="text-xs">
                    {j.params.start} → {j.params.end}
                  </td>
                  <td>{j.params.include_walk_forward ? "✓" : ""}</td>
                  <td>
                    <Chip tone={statusTone(j.status)}>{j.status}</Chip>
                  </td>
                  <td className="text-xs text-slate-500">{fmtTime(j.submitted_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="p-4 text-center text-sm text-slate-500">No backtest runs yet.</p>
        )}
      </Card>

      {selected && <ReportPanel runId={selected} onChanged={() => qc.invalidateQueries()} />}
    </div>
  );
}

function SubmitForm({ onSubmitted }: { onSubmitted: (runId: string) => void }) {
  const markets = useQuery({ queryKey: ["markets"], queryFn: fetchMarkets });
  const mutation = useMutation({
    mutationFn: (payload: BacktestStartPayload) => submitBacktest(payload),
    onSuccess: (job) => onSubmitted(job.run_id),
  });

  const today = new Date().toISOString().slice(0, 10);
  const tenDaysAgo = new Date(Date.now() - 10 * 86400_000).toISOString().slice(0, 10);
  const [assetId, setAssetId] = useState("");
  const [start, setStart] = useState(tenDaysAgo);
  const [end, setEnd] = useState(today);
  const [wf, setWf] = useState(true);

  useEffect(() => {
    if (!assetId && markets.data && markets.data.length > 0) {
      setAssetId(markets.data[0].spec.asset_id);
    }
  }, [markets.data, assetId]);

  return (
    <Card title="New run (baselines only — AI strategies are explicit opt-in and cost money)">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!assetId || !start || !end || start >= end) return;
          mutation.mutate({
            asset_id: assetId,
            timeframe: "1h",
            start,
            end,
            include_walk_forward: wf,
          });
        }}
        className="flex flex-wrap items-end gap-3 text-sm"
      >
        <label className="flex flex-col gap-1 text-xs text-slate-500">
          asset
          <select
            value={assetId}
            onChange={(e) => setAssetId(e.target.value)}
            className="rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-slate-200"
          >
            {(markets.data ?? []).map((m) => (
              <option key={m.spec.asset_id} value={m.spec.asset_id}>
                {m.spec.asset_id}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-500">
          start
          <input
            type="date"
            value={start}
            onChange={(e) => setStart(e.target.value)}
            className="rounded border border-slate-700 bg-slate-950 px-2 py-1.5"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-500">
          end
          <input
            type="date"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            className="rounded border border-slate-700 bg-slate-950 px-2 py-1.5"
          />
        </label>
        <label className="flex items-center gap-2 pb-2 text-xs text-slate-400">
          <input type="checkbox" checked={wf} onChange={(e) => setWf(e.target.checked)} />
          walk-forward
        </label>
        <button
          type="submit"
          disabled={mutation.isPending}
          className="rounded bg-sky-800 px-4 py-2 text-xs font-semibold text-white hover:bg-sky-700 disabled:opacity-40"
        >
          {mutation.isPending ? "submitting…" : "run"}
        </button>
      </form>
      {mutation.error && (
        <div className="mt-3">
          <ErrorNote error={mutation.error} />
        </div>
      )}
    </Card>
  );
}

function ReportPanel({ runId, onChanged }: { runId: string; onChanged: () => void }) {
  // Poll while the job is queued/running; stop once terminal.
  const detail = useQuery({
    queryKey: ["backtest", runId],
    queryFn: () => fetchBacktest(runId),
    refetchInterval: (q) =>
      q.state.data && ["completed", "failed"].includes(q.state.data.job.status) ? false : 1500,
  });

  useEffect(() => {
    if (detail.data && ["completed", "failed"].includes(detail.data.job.status)) onChanged();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fire once per terminal state
  }, [detail.data?.job.status]);

  if (detail.isPending) return <Loading label="loading run…" />;
  if (detail.error) return <ErrorNote error={detail.error} />;
  const { job, report } = detail.data;

  return (
    <Card title={`Run ${job.run_id}`}>
      {job.status === "failed" ? (
        /* Honest failure surfacing is a spec requirement (§26). */
        <div className="space-y-2">
          <Chip tone="negative">failed</Chip>
          <p className="font-mono text-xs text-red-300">{job.error}</p>
          <p className="text-xs text-slate-500">
            The dataset for this window may not exist yet — fetch it via the CLI first:
            <code className="ml-2 rounded bg-slate-900 px-2 py-0.5 text-sky-300">
              tradingagents data fetch -a {job.params.asset_id} -t {job.params.timeframe}
            </code>
          </p>
        </div>
      ) : report ? (
        <div className="space-y-6">
          <StrategiesTable rows={report.strategies} />
          {report.walk_forward.length > 0 && (
            <section className="space-y-3">
              <h3 className="text-xs uppercase tracking-wider text-slate-500">
                Walk-forward ({report.walk_forward.length} strategies)
              </h3>
              {report.walk_forward.map((wf) => (
                <div key={wf.strategy_id} className="space-y-2 rounded border border-slate-800 p-3">
                  <div className="flex items-baseline gap-3">
                    <span className="font-mono text-sm text-slate-300">{wf.strategy_id}</span>
                    <span className="text-xs text-slate-500">
                      {wf.aggregate.profitable_windows}/{wf.aggregate.n_windows} windows
                      profitable · avg{" "}
                      {fmtPct(wf.aggregate.average_window_return_pct)} · best{" "}
                      {fmtPct(wf.aggregate.best_window_return_pct)} · worst{" "}
                      {fmtPct(wf.aggregate.worst_window_return_pct)}
                    </span>
                    {wf.aggregate.aggregate_return_pct != null && (
                      <span
                        className={
                          wf.aggregate.aggregate_return_pct >= 0
                            ? "ml-auto font-mono text-xs text-emerald-300"
                            : "ml-auto font-mono text-xs text-red-300"
                        }
                      >
                        aggregate {fmtPct(wf.aggregate.aggregate_return_pct)}
                      </span>
                    )}
                  </div>
                  <WalkForwardWindowsTable windows={wf.windows} />
                </div>
              ))}
            </section>
          )}
        </div>
      ) : (
        <div className="flex items-center gap-3">
          <span className="h-3 w-3 animate-spin rounded-full border-2 border-sky-500 border-t-transparent" />
          <span className="text-sm text-slate-400">
            status: {job.status} — this panel polls until the worker finishes
          </span>
        </div>
      )}
    </Card>
  );
}

function StrategiesTable({ rows }: { rows: StrategyResult[] }) {
  return (
    <table className="w-full font-mono text-sm">
      <thead>
        <tr className="text-left text-[11px] uppercase tracking-wider text-slate-500">
          <th className="py-1.5">Strategy</th>
          <th className="py-1.5">Return</th>
          <th className="py-1.5">Sharpe</th>
          <th className="py-1.5">Max DD</th>
          <th className="py-1.5">Win rate</th>
          <th className="py-1.5">Trades</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const s = r.stats;
          return (
            <tr key={r.strategy_id} className="border-t border-slate-800/60">
              <td className="py-1.5 text-slate-300">{r.strategy_id}</td>
              <td className={s.total_return_pct >= 0 ? "text-emerald-300" : "text-red-300"}>
                {fmtPct(s.total_return_pct)}
              </td>
              <td>{s.sharpe_ratio?.toFixed(2) ?? "—"}</td>
              <td className="text-red-300">{fmtPct(s.max_drawdown_pct, 2)}</td>
              <td>{s.win_rate_pct != null ? `${s.win_rate_pct.toFixed(0)}%` : "—"}</td>
              <td>{s.n_trades}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function WalkForwardWindowsTable({ windows }: { windows: WalkForwardWindowMetrics[] }) {
  return (
    <table className="w-full font-mono text-xs">
      <thead>
        <tr className="text-left text-[11px] uppercase tracking-wider text-slate-600">
          <th className="py-1">#</th>
          <th className="py-1">Train</th>
          <th className="py-1">Test</th>
          <th className="py-1">Return</th>
          <th className="py-1">Max DD</th>
          <th className="py-1">Trades</th>
          <th className="py-1">Note</th>
        </tr>
      </thead>
      <tbody>
        {windows.map((w) => (
          <tr key={`${w.strategy_id}-${w.window_id}`} className="border-t border-slate-800/40">
            <td className="py-1">{w.window_id}</td>
            <td className="text-slate-500">
              {w.train_period ? `${w.train_period[0].slice(0, 10)} → ${w.train_period[1].slice(0, 10)}` : "—"}
            </td>
            <td className="text-slate-500">
              {w.test_period ? `${w.test_period[0].slice(0, 10)} → ${w.test_period[1].slice(0, 10)}` : "—"}
            </td>
            <td className={w.total_return_pct >= 0 ? "text-emerald-300" : "text-red-300"}>
              {fmtPct(w.total_return_pct)}
            </td>
            <td className="text-red-300">{fmtPct(w.max_drawdown_pct, 2)}</td>
            <td>{w.trades}</td>
            <td className="text-slate-600">{w.skipped_reason ?? ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
