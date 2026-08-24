import { useEffect, useRef } from "react";
import {
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Candle, EquityPoint } from "../api/types";

export interface ChartOverlay {
  kind: string;
  values:
    | { t: string; value: number }[]
    | { t: string; bands: Record<string, number | undefined> }[];
}

const DARK = {
  layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#94a3b8" },
  grid: {
    vertLines: { color: "#1e293b66" },
    horzLines: { color: "#1e293b66" },
  },
};

function toTs(iso: string): UTCTimestamp {
  return Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp;
}

/** Candlestick chart with optional SMA/EMA/BB overlays (spec §10). */
export function CandleChart({
  candles,
  indicators = [],
  height = 380,
}: {
  candles: Candle[];
  indicators?: ChartOverlay[];
  height?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, { height, autoSize: true, ...DARK });
    chartRef.current = chart;
    candleRef.current = chart.addCandlestickSeries({
      upColor: "#059669",
      downColor: "#dc2626",
      wickUpColor: "#059669",
      wickDownColor: "#dc2626",
    });
    return () => {
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
    };
  }, [height]);

  useEffect(() => {
    const series = candleRef.current;
    if (!series) return;
    series.setData(
      candles.map((c) => ({
        time: toTs(c.t),
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      })),
    );
    chartRef.current?.timeScale().fitContent();
  }, [candles]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    // Rebuild overlay panes on every indicator change; counts are tiny.
    for (const ind of indicators) {
      if (ind.kind === "bbands") {
        const upper = chart.addLineSeries({ color: "#64748b", lineWidth: 1 });
        const lower = chart.addLineSeries({ color: "#64748b", lineWidth: 1 });
        const ptsU: { time: UTCTimestamp; value: number }[] = [];
        const ptsL: { time: UTCTimestamp; value: number }[] = [];
        for (const v of ind.values) {
          if ("bands" in v && v.bands.upper !== undefined) ptsU.push({ time: toTs(v.t), value: v.bands.upper });
          if ("bands" in v && v.bands.lower !== undefined) ptsL.push({ time: toTs(v.t), value: v.bands.lower });
        }
        upper.setData(ptsU);
        lower.setData(ptsL);
      } else {
        const line = chart.addLineSeries({ color: ind.kind === "ema" ? "#f59e0b" : "#38bdf8", lineWidth: 2 });
        line.setData(
          ind.values
            .filter((v): v is { t: string; value: number } => "value" in v)
            .map((v) => ({ time: toTs(v.t), value: v.value })),
        );
      }
    }
  }, [indicators]);

  return <div ref={ref} data-testid="candle-chart" className="w-full" />;
}

/** Equity curve as a filled area; chronological, backend-ordered. */
export function EquityChart({ points, height = 300 }: { points: EquityPoint[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, { height, autoSize: true, ...DARK });
    const series = chart.addAreaSeries({
      lineColor: "#38bdf8",
      topColor: "#38bdf833",
      bottomColor: "#38bdf811",
    });
    series.setData(points.map((pt) => ({ time: toTs(pt.timestamp), value: pt.equity })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points, height]);

  return <div ref={ref} data-testid="equity-chart" className="w-full" />;
}

/** Volume histogram under a candle chart (kept simple: standalone pane). */
export function VolumeChart({ candles, height = 90 }: { candles: Candle[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current || !candles.length) return;
    const chart = createChart(ref.current, { height, autoSize: true, ...DARK });
    const vol = chart.addHistogramSeries({ priceFormat: { type: "volume" } });
    vol.setData(
      candles
        .filter((c) => c.volume !== null)
        .map((c) => ({
          time: toTs(c.t),
          value: c.volume as number,
          color: c.close >= c.open ? "#05966955" : "#dc262655",
        })),
    );
    return () => chart.remove();
  }, [candles, height]);

  return <div ref={ref} className="w-full" />;
}
