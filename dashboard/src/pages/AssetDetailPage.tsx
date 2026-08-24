import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams, Link } from "react-router-dom";
import { fetchAsset, fetchCandles, fetchIndicators, fetchResearch } from "../api/hooks";
import type { IndicatorSeriesOut } from "../api/types";
import { Card, Chip, ErrorNote, Loading, statusTone } from "../components/ui";
import { CandleChart } from "../components/charts";
import { fmtMoney, fmtTime } from "../lib/format";

const TF_OPTIONS = ["15m", "1h", "4h", "1d"] as const;
const OVERLAYS = ["none", "sma20", "ema50", "bb20"] as const;

/** Convert the backend's timestamps+values arrays into chart-ready points. */
function toPoints(
  ts: string[],
  series: IndicatorSeriesOut,
): { t: string; value: number }[] {
  return series.values
    .map((v, i) => ({ t: ts[i], value: v }))
    .filter((p): p is { t: string; value: number } => p.value !== null && p.t !== undefined);
}

export function AssetDetailPage() {
  const { assetId = "" } = useParams();
  const [timeframe, setTimeframe] = useState<(typeof TF_OPTIONS)[number]>("1h");
  const [overlay, setOverlay] = useState<(typeof OVERLAYS)[number]>("sma20");

  const asset = useQuery({ queryKey: ["asset", assetId], queryFn: () => fetchAsset(assetId) });
  const candles = useQuery({
    queryKey: ["candles", assetId, timeframe],
    queryFn: () => fetchCandles(assetId, timeframe, 300),
  });
  const indicators = useQuery({
    queryKey: ["indicators", assetId, timeframe, overlay],
    queryFn: () => fetchIndicators(assetId, timeframe, overlay),
    enabled: overlay !== "none",
  });
  const research = useQuery({
    queryKey: ["research", assetId],
    queryFn: () => fetchResearch({ asset_id: assetId, limit: 5 }),
  });

  const overlays = useMemo(() => {
    if (overlay === "none" || !indicators.data) return [] as { name: string; points: { t: string; value: number }[] }[];
    return indicators.data.series
      .filter((s) => !s.name.endsWith("-upper") && !s.name.endsWith("-lower"))
      .map((s) => ({ name: s.name, points: toPoints(indicators.data!.timestamps, s) }));
  }, [overlay, indicators.data]);

  const bbands = useMemo(() => {
    if (overlay === "none" || !indicators.data) return [];
    const upper = indicators.data.series.find((s) => s.name.endsWith("-upper"));
    const lower = indicators.data.series.find((s) => s.name.endsWith("-lower"));
    if (!upper || !lower) return [] as { name: string; points: { t: string; value: number }[] }[];
    return [
      { name: upper.name, points: toPoints(indicators.data.timestamps, upper) },
      { name: lower.name, points: toPoints(indicators.data.timestamps, lower) },
    ];
  }, [overlay, indicators.data]);

  if (asset.isPending) return <Loading />;
  if (asset.error) return <ErrorNote error={asset.error} />;

  const naReason =
    overlay !== "none" && indicators.data && Object.keys(indicators.data.na_reasons).length > 0
      ? Object.values(indicators.data.na_reasons).join("; ")
      : null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-lg font-semibold">{asset.data.spec.display_name}</h1>
        <span className="font-mono text-sm text-slate-500">{asset.data.spec.asset_id}</span>
        <Chip tone="neutral">{asset.data.spec.asset_class}</Chip>
        <Chip tone={asset.data.freshness === "fresh" ? "positive" : "warning"}>
          {asset.data.quote ? `${asset.data.quote.source} · ${asset.data.quote.data_status}` : asset.data.note}
        </Chip>
        <span className="ml-auto flex gap-2">
          {TF_OPTIONS.map((tf) => (
            <button
              key={tf}
              onClick={() => setTimeframe(tf)}
              className={
                tf === timeframe
                  ? "rounded bg-slate-700 px-3 py-1 text-xs font-semibold text-white"
                  : "rounded px-3 py-1 text-xs text-slate-400 hover:bg-slate-800"
              }
            >
              {tf}
            </button>
          ))}
        </span>
      </div>

      <div className="flex items-center gap-2 text-xs text-slate-500">
        overlay:
        {OVERLAYS.map((kind) => (
          <button
            key={kind}
            onClick={() => setOverlay(kind)}
            className={
              kind === overlay
                ? "rounded bg-sky-900/60 px-2 py-0.5 font-semibold text-sky-200"
                : "rounded px-2 py-0.5 hover:bg-slate-800"
            }
          >
            {kind}
          </button>
        ))}
        {naReason && <span className="text-amber-400">({naReason})</span>}
        <span className="ml-auto font-mono text-sm text-slate-300">
          last {fmtMoney(asset.data.quote?.last)}
        </span>
      </div>

      <Card>
        {candles.isPending ? (
          <Loading label="loading candles…" />
        ) : candles.error ? (
          <ErrorNote error={candles.error} />
        ) : (
          <CandleChart candles={candles.data?.bars ?? []} indicators={
            // map simple line overlays into the chart's expected shape
            [
              ...overlays.map((o) => ({
                kind: o.name.startsWith("ema") ? "ema" : "sma",
                values: o.points,
              })),
              ...(bbands.length === 2
                ? [
                    {
                      kind: "bbands",
                      values: bbands[0].points.map((p, i) => ({
                        t: p.t,
                        bands: { upper: p.value, lower: bbands[1].points[i]?.value },
                      })),
                    },
                  ]
                : []),
            ]
          } />
        )}
      </Card>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card title="Instrument details">
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 font-mono text-sm">
            <dt className="text-slate-500">quote currency</dt>
            <dd>{asset.data.spec.quote_currency}</dd>
            <dt className="text-slate-500">last quote</dt>
            <dd>{fmtTime(asset.data.quote?.timestamp)}</dd>
            <dt className="text-slate-500">bars loaded</dt>
            <dd>{candles.data?.bars.length ?? 0} · {candles.data?.source}</dd>
            <dt className="text-slate-500">scheduled slots</dt>
            <dd>{asset.data.scheduled_slots.filter((s) => s.enabled).length}</dd>
          </dl>
          {asset.data.latest_signal_ref && (
            <p className="mt-3 border-t border-slate-800 pt-3 text-xs text-slate-400">
              latest signal{" "}
              <Link to={`/signals/${asset.data.latest_signal_ref.signal_id}`} className="text-sky-400 hover:underline">
                {asset.data.latest_signal_ref.signal_id}
              </Link>{" "}
              ({asset.data.latest_signal_ref.action} {(asset.data.latest_signal_ref.confidence * 100).toFixed(0)}%)
            </p>
          )}
        </Card>

        <Card title="Recent research runs">
          {research.isPending ? (
            <Loading />
          ) : research.data && research.data.items.length > 0 ? (
            <ul className="space-y-2 text-sm">
              {research.data.items.map((r) => (
                <li key={r.run_id} className="flex items-center gap-2">
                  <Chip tone={statusTone(r.signal_action ?? "")}>{r.signal_action ?? "no signal"}</Chip>
                  <span className="font-mono text-[11px] text-slate-400">{fmtTime(r.generated_at)}</span>
                  {r.models_used.length > 0 && (
                    <span className="truncate text-[11px] text-slate-600">{r.models_used.join(", ")}</span>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-slate-500">No research runs recorded for this asset.</p>
          )}
        </Card>
      </div>
    </div>
  );
}
