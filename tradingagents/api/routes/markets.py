"""Market endpoints: registry overview, detail, candles, indicator overlays.

All values (quotes, changes, freshness, indicators) are computed here from
provider/indicator data — the browser only renders (spec §6/§45).
"""

from __future__ import annotations

import contextlib
import math
from datetime import datetime, timedelta, timezone
from typing import Annotated

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from tradingagents.api.context import AppContext
from tradingagents.assets.registry import UnknownAssetError, get_asset, list_assets
from tradingagents.marketdata.models import OhlcvSeries
from tradingagents.marketdata.timeframes import Timeframe

router = APIRouter(tags=["markets"])


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


CtxDep = Annotated[AppContext, Depends(get_ctx)]


def _resolve_asset(asset_id: str):
    try:
        return get_asset(asset_id.strip().upper())
    except (UnknownAssetError, KeyError) as exc:
        raise HTTPException(status_code=404, detail="unknown asset") from exc


def _resolve_tf(value: str) -> Timeframe:
    try:
        return Timeframe(value.strip().lower())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported timeframe {value!r}; expected one of "
            f"{[t.value for t in Timeframe]}",
        ) from exc


def _freshness(series: OhlcvSeries) -> str:
    """FRESH until the newest bar's effective close is older than the
    timeframe's documented staleness window (same rule the engines use)."""
    if not series.bars:
        return "unknown"
    effective_close = (
        series.bars[-1].timestamp.replace(tzinfo=timezone.utc)
        + timedelta(minutes=series.timeframe.minutes)
    )
    age_hours = (datetime.now(timezone.utc) - effective_close).total_seconds() / 3600
    return "fresh" if age_hours <= series.timeframe.staleness_hours() else "stale"


def _change(series: OhlcvSeries) -> tuple[float | None, float | None]:
    closes = [bar.close for bar in series.bars]
    if len(closes) < 2 or not closes[-2]:
        return None, None
    change = closes[-1] - closes[-2]
    pct = change / closes[-2] * 100
    return round(change, 8), round(pct, 4)


def _quote_out(quote):
    from tradingagents.api.schemas import QuoteOut

    if quote is None:
        return None
    return QuoteOut(
        asset_id=quote.asset_id,
        timestamp=quote.timestamp,
        last=quote.last,
        bid=quote.bid,
        ask=quote.ask,
        source=quote.source,
        data_status=(
            quote.status.value if hasattr(quote.status, "value") else str(quote.status)
        ),
    )


@router.get("/markets")
def market_overview(
    ctx: CtxDep,
    tf_value: Annotated[str, Query(alias="tf", description="change window")] = "1d",
):
    from tradingagents.api.schemas import AssetSummary, MarketOverviewItem

    tf = _resolve_tf(tf_value)
    items = []
    for spec in list_assets():
        note = ""
        try:
            quote_out = _quote_out(ctx.provider().get_quote(spec))
            if quote_out is None:
                note = "no quote available"
        except Exception as exc:
            quote_out, note = None, f"{type(exc).__name__}: {exc}"[:200]
        change_abs = change_pct = None
        with contextlib.suppress(Exception):
            change_abs, change_pct = _change(ctx.provider().get_ohlcv(spec, tf, limit=2))
        items.append(
            MarketOverviewItem(
                spec=AssetSummary(
                    asset_id=spec.asset_id,
                    display_name=spec.display_name,
                    asset_class=spec.asset_class.value,
                    quote_currency=spec.quote_currency,
                ),
                quote=quote_out,
                change_abs=change_abs,
                change_pct=change_pct,
                change_timeframe=tf.value,
                freshness="unknown",
                note=note,
            )
        )
    return items


@router.get("/markets/{asset_id}")
def market_detail(
    asset_id: str,
    ctx: CtxDep,
    tf_value: Annotated[str, Query(alias="tf")] = "1h",
):
    from tradingagents.api.schemas import (
        AssetSummary,
        MarketDetailResponse,
        ResearchRunRef,
        ScheduleSlotView,
        SignalRef,
    )

    spec = _resolve_asset(asset_id)
    tf = _resolve_tf(tf_value)

    note = ""
    try:
        quote_out = _quote_out(ctx.provider().get_quote(spec))
        if quote_out is None:
            note = "no quote available"
    except Exception as exc:
        quote_out, note = None, f"{type(exc).__name__}: {exc}"[:200]

    change_abs = change_pct = None
    freshness = "unknown"
    try:
        series = ctx.provider().get_ohlcv(spec, tf, limit=2)
        change_abs, change_pct = _change(series)
        freshness = _freshness(series)
    except Exception:
        pass

    runs, _ = ctx.research_store.list_runs(asset_id=spec.asset_id, limit=1)
    latest_run = (
        ResearchRunRef(
            run_id=runs[0].run_id,
            generated_at=runs[0].generated_at,
            signal_action=runs[0].signal_action,
            confidence=runs[0].confidence,
        )
        if runs
        else None
    )

    signals = [s for s in ctx.store().list_signals() if s.asset_id == spec.asset_id]
    signals.sort(key=lambda s: s.generated_at, reverse=True)
    latest_signal = (
        SignalRef(
            signal_id=signals[0].signal_id,
            state=signals[0].state.value,
            action=signals[0].action.value,
            confidence=signals[0].confidence,
            generated_at=signals[0].generated_at,
        )
        if signals
        else None
    )

    cfg = ctx.server_config()
    store = ctx.store()
    slots = []
    for entry in cfg.schedules:
        if entry.asset_id != spec.asset_id:
            continue
        state = store.load_schedule_state(
            f"{entry.asset_id.upper()}:{entry.timeframe}"
        ) or {}
        slots.append(
            ScheduleSlotView(
                asset_id=entry.asset_id,
                timeframe=entry.timeframe,
                enabled=entry.enabled and cfg.enabled,
                next_run_at=state.get("next_run_at"),
                last_processed_bar_close=state.get("last_processed_bar_close"),
            )
        )

    return MarketDetailResponse(
        spec=AssetSummary(
            asset_id=spec.asset_id,
            display_name=spec.display_name,
            asset_class=spec.asset_class.value,
            quote_currency=spec.quote_currency,
        ),
        quote=quote_out,
        change_abs=change_abs,
        change_pct=change_pct,
        change_timeframe=tf.value,
        freshness=freshness,
        note=note,
        latest_research_run=latest_run,
        latest_signal_ref=latest_signal,
        scheduled_slots=slots,
    )


@router.get("/markets/{asset_id}/candles")
def candles(
    asset_id: str,
    ctx: CtxDep,
    timeframe: str = "1h",
    limit: Annotated[int, Query(ge=1, le=1500)] = 300,
    start: datetime | None = None,
    end: datetime | None = None,
):
    from tradingagents.api.schemas import CandleOut, CandlesResponse

    spec = _resolve_asset(asset_id)
    tf = _resolve_tf(timeframe)
    try:
        series = ctx.provider().get_ohlcv(
            spec,
            tf,
            limit=limit,
            start=start.replace(tzinfo=timezone.utc) if start else None,
            end=end.replace(tzinfo=timezone.utc) if end else None,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"market data unavailable: {exc}"
        ) from exc
    return CandlesResponse(
        asset_id=series.asset_id,
        timeframe=series.timeframe.value,
        source=series.source,
        data_status=(
            series.status.value if hasattr(series.status, "value") else str(series.status)
        ),
        freshness=_freshness(series),
        bars=[
            CandleOut(
                t=b.timestamp,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
            )
            for b in series.bars
        ],
    )


@router.get("/markets/{asset_id}/indicators")
def indicators(
    asset_id: str,
    ctx: CtxDep,
    timeframe: str = "1h",
    kinds: Annotated[
        str, Query(description="csv of sma{n} | ema{n} | bb{n}[-upper|-mid|-lower]")
    ] = "ema20,sma50",
    limit: Annotated[int, Query(ge=30, le=1500)] = 300,
):
    from tradingagents.api.schemas import IndicatorSeries, IndicatorsResponse

    spec = _resolve_asset(asset_id)
    tf = _resolve_tf(timeframe)
    try:
        series = ctx.provider().get_ohlcv(spec, tf, limit=limit)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"market data unavailable: {exc}"
        ) from exc

    closes = pd.Series([b.close for b in series.bars], dtype=float)

    def cleaned(values: pd.Series) -> list[float | None]:
        return [
            None if v is None or math.isnan(v) else round(float(v), 8)
            for v in values.tolist()
        ]

    out_series: list[IndicatorSeries] = []
    na_reasons: dict[str, str] = {}
    for raw in [k.strip().lower() for k in kinds.split(",") if k.strip()]:
        kind, rest, band = "", raw, ""
        for prefix in ("sma", "ema", "bb"):
            if raw.startswith(prefix):
                kind, rest = prefix, raw[len(prefix):]
                break
        if "-" in rest:
            rest, band = rest.split("-", 1)
        if kind == "bb":
            period = int(rest) if rest.isdigit() and int(rest) >= 2 else 20
            mid = closes.rolling(period).mean()
            sd = closes.rolling(period).std(ddof=0)
            bands = {
                "": mid + 2 * sd,
                "upper": mid + 2 * sd,
                "mid": mid,
                "lower": mid - 2 * sd,
            }
            if band not in bands:
                na_reasons[raw] = f"unknown band {band!r}"
                continue
            out_series.append(IndicatorSeries(name=raw, values=cleaned(bands[band])))
        elif kind in ("sma", "ema"):
            if not (rest.isdigit() and int(rest) >= 2):
                na_reasons[raw] = "period must be an integer >= 2"
                continue
            period = int(rest)
            values = (
                closes.ewm(span=period, adjust=False).mean()
                if kind == "ema"
                else closes.rolling(period).mean()
            )
            out_series.append(IndicatorSeries(name=raw, values=cleaned(values)))
        else:
            na_reasons[raw] = "unsupported kind"
    return IndicatorsResponse(
        asset_id=series.asset_id,
        timeframe=tf.value,
        timestamps=[b.timestamp for b in series.bars],
        series=out_series,
        na_reasons=na_reasons,
    )


__all__ = ["router"]
