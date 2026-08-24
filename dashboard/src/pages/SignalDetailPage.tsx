import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { fetchSignal } from "../api/hooks";
import { Card, Chip, ErrorNote, Loading, statusTone } from "../components/ui";
import { fmtMoney, fmtTime } from "../lib/format";

export function SignalDetailPage() {
  const { signalId = "" } = useParams();
  const signal = useQuery({ queryKey: ["signal", signalId], queryFn: () => fetchSignal(signalId) });

  if (signal.isPending) return <Loading />;
  if (signal.error) return <ErrorNote error={signal.error} />;
  const { record: s, transitions, orders, research_run } = signal.data;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/signals" className="text-xs text-slate-500 hover:text-sky-400">
          ← signals
        </Link>
        <h1 className="font-mono text-lg">{s.signal_id}</h1>
        <Chip tone={statusTone(s.state)}>{s.state}</Chip>
        <span className="text-sm text-slate-500">
          {s.asset_id} · {s.action} @ confidence {(s.confidence * 100).toFixed(0)}%
        </span>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card title="Signal parameters">
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 font-mono text-sm">
            <dt className="text-slate-500">entry reference</dt>
            <dd>{fmtMoney(s.entry_reference)}</dd>
            <dt className="text-slate-500">stop loss</dt>
            <dd>{fmtMoney(s.stop_loss_reference)}</dd>
            <dt className="text-slate-500">take profit</dt>
            <dd>{fmtMoney(s.take_profit_reference)}</dd>
            <dt className="text-slate-500">decision bar</dt>
            <dd>{fmtTime(s.decision_bar_close)}</dd>
            <dt className="text-slate-500">generated</dt>
            <dd>{fmtTime(s.generated_at)}</dd>
          </dl>
          {s.thesis && (
            <p className="mt-3 border-t border-slate-800 pt-3 text-xs leading-relaxed text-slate-400">
              {s.thesis}
            </p>
          )}
          {s.rejection_reason && (
            <p className="mt-2 text-xs text-red-300">rejection: {s.rejection_reason}</p>
          )}
        </Card>

        <Card title="Lifecycle transitions">
          <ol className="space-y-1 text-sm">
            {transitions.map((t, i) => (
              <li
                key={`${t.from_state}-${t.to_state}-${i}`}
                className="flex items-center gap-2 rounded bg-slate-900/60 px-3 py-1.5"
              >
                <span aria-hidden className="h-2 w-2 rounded-full bg-emerald-400" />
                <span className="font-mono text-xs text-slate-400">
                  {t.from_state} → <span className="text-slate-200">{t.to_state}</span>
                </span>
                <span className="font-mono text-[11px] text-slate-500">{fmtTime(t.ts)}</span>
                {t.reason && (
                  <span className="ml-auto truncate text-[11px] text-slate-600">{t.reason}</span>
                )}
              </li>
            ))}
            {orders.length > 0 && (
              <li className="rounded bg-slate-900/60 px-3 py-1.5 font-mono text-xs text-slate-500">
                {orders.length} paper order(s) linked
              </li>
            )}
          </ol>
        </Card>
      </div>

      <Card title={`Research attribution${research_run ? ` — ${research_run.run_id}` : ""}`}>
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <section>
            <h3 className="mb-1 text-xs uppercase tracking-wider text-emerald-400">
              Bull / supporting factors
            </h3>
            <ul className="list-inside list-disc text-xs leading-relaxed text-slate-400">
              {(s.research.bull_case ? [s.research.bull_case] : []).concat(s.supporting_factors).map((f, i) => (
                <li key={i}>{f}</li>
              ))}
            </ul>
          </section>
          <section>
            <h3 className="mb-1 text-xs uppercase tracking-wider text-red-400">
              Bear / opposing factors
            </h3>
            <ul className="list-inside list-disc text-xs leading-relaxed text-slate-400">
              {(s.research.bear_case ? [s.research.bear_case] : []).concat(s.opposing_factors).map((f, i) => (
                <li key={i}>{f}</li>
              ))}
            </ul>
          </section>
          <section>
            <h3 className="mb-1 text-xs uppercase tracking-wider text-amber-400">
              Invalidation conditions
            </h3>
            <ul className="list-inside list-disc text-xs leading-relaxed text-slate-400">
              {s.invalidation_conditions.map((f, i) => (
                <li key={i}>{f}</li>
              ))}
            </ul>
            <div className="mt-3 flex flex-wrap gap-1">
              {s.models_used.map((m) => (
                <Chip key={m}>{m}</Chip>
              ))}
            </div>
          </section>
        </div>
      </Card>
    </div>
  );
}
