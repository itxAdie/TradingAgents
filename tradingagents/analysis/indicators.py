"""Deterministic technical-indicator engine.

Computes a fixed initial indicator set (RSI, MACD, SMA20/50/200, EMA10,
Bollinger Bands, ATR, price momentum, realized volatility) directly from a
normalized :class:`~tradingagents.marketdata.models.OhlcvSeries` with
explicit pandas expressions, so every window and smoothing convention is
visible in code rather than delegated to an indicator library's defaults.

Contract:

- Output is structured (:class:`TechnicalSnapshot`), never markdown — this is
  ground truth consumed by agents and by deterministic signal assembly.
- Insufficient lookback yields ``None`` plus an explicit reason; NaN is never
  silently propagated as a value.
- Pure function of its inputs: same series in, same snapshot out.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from tradingagents.marketdata.models import OhlcvSeries
from tradingagents.marketdata.timeframes import Timeframe

# Minimum rows each indicator needs before its value is trustworthy.
_REQUIRED_LOOKBACK: dict[str, int] = {
    "ema_10": 10,
    "sma_20": 20,
    "sma_50": 50,
    "sma_200": 200,
    "rsi_14": 15,
    "macd": 35,  # fast 12 + slow 26 signal warm-up
    "macd_signal": 35,
    "macd_hist": 35,
    "boll_mid": 20,
    "boll_upper": 20,
    "boll_lower": 20,
    "atr_14": 15,
}

_MOMENTUM_LOOKBACK = 10
_VOLATILITY_WINDOW = 100
_VOLATILITY_MIN_BARS = 21

# ATR/close ratio buckets used for the volatility label (and later risk level).
ATR_PCT_HIGH = 0.03
ATR_PCT_MEDIUM = 0.012

# Annualization factors for realized volatility (documented approximation):
# crypto trades ~365d continuously; metals/index/equity venues use a 252-day,
# ~23h futures session convention.
_MINUTES_PER_YEAR_CRYPTO = 365 * 24 * 60
_MINUTES_PER_YEAR_VENUE = 252 * 23 * 60


class TechnicalSnapshot(BaseModel):
    """Ground-truth numeric picture of one asset/timeframe."""

    asset_id: str
    timeframe: Timeframe
    computed_at: datetime
    bar_count: int
    first_bar_timestamp: datetime | None = None
    latest_close: float | None = None
    latest_bar_timestamp: datetime | None = None
    # name -> value or None when insufficient lookback / not computable
    indicators: dict[str, float | None] = Field(default_factory=dict)
    # name -> human-readable reason why the value is None
    missing_reasons: dict[str, str] = Field(default_factory=dict)
    trend: str = "unknown"  # up | down | sideways | unknown
    momentum: str = "unknown"  # bullish | bearish | neutral | unknown
    volatility: str = "unknown"  # low | medium | high | unknown


def _series_frame(series: OhlcvSeries) -> pd.DataFrame:
    frame = pd.DataFrame([bar.to_row() for bar in series.bars])
    frame["Date"] = pd.to_datetime(frame["Date"], utc=True)
    return frame


def _clean(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _realized_volatility(closes: pd.Series, minutes_per_year: int) -> tuple[float | None, int]:
    window = closes.tail(_VOLATILITY_WINDOW)
    n_bars = len(window)
    if n_bars < _VOLATILITY_MIN_BARS:
        return None, n_bars
    returns = window.pct_change().dropna()
    if returns.empty:
        return None, n_bars
    per_bar_sigma = float(returns.std(ddof=1))
    if math.isnan(per_bar_sigma):
        return None, n_bars
    bars_per_year = minutes_per_year / max(1, _bar_minutes_from_span(window))
    annualized = per_bar_sigma * math.sqrt(bars_per_year)
    return annualized, n_bars


def _bar_minutes_from_span(window: pd.Series) -> int:
    """Median spacing between the last bars, in whole minutes."""
    stamps = window.index
    if len(stamps) < 2:
        return 1
    deltas = pd.Series(stamps).diff().dropna()
    median_delta = deltas.median()
    minutes = median_delta.total_seconds() / 60 if hasattr(median_delta, "total_seconds") else 1
    return max(1, int(round(minutes)))


def classify_trend(
    close: float | None, sma50: float | None, sma200: float | None
) -> str:
    if close is None or sma50 is None:
        return "unknown"
    if sma200 is not None:
        if close > sma50 and close > sma200:
            return "up"
        if close < sma50 and close < sma200:
            return "down"
        return "sideways"
    if close > sma50:
        return "up"
    if close < sma50:
        return "down"
    return "sideways"


def classify_momentum(macd_hist: float | None, rsi: float | None) -> str:
    if macd_hist is not None:
        if macd_hist > 0:
            return "bullish"
        if macd_hist < 0:
            return "bearish"
        return "neutral"
    if rsi is not None:
        if rsi >= 55:
            return "bullish"
        if rsi <= 45:
            return "bearish"
        return "neutral"
    return "unknown"


def classify_volatility(atr: float | None, close: float | None) -> str:
    if atr is None or not close:
        return "unknown"
    atr_pct = atr / close
    if atr_pct >= ATR_PCT_HIGH:
        return "high"
    if atr_pct >= ATR_PCT_MEDIUM:
        return "medium"
    return "low"


def _wilder(values: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (EMA with ``alpha = 1/period``), as in classic TA."""
    return values.ewm(alpha=1 / period, adjust=False).mean()


def _indicator_series(name: str, df: pd.DataFrame) -> pd.Series:
    """Compute one indicator column by name (windows fixed, see constants)."""
    close, high, low = df["Close"], df["High"], df["Low"]
    if name == "ema_10":
        return close.ewm(span=10, adjust=False).mean()
    if name.startswith("sma_"):
        return close.rolling(int(name.split("_")[1])).mean()
    if name == "rsi_14":
        delta = close.diff()
        gain = _wilder(delta.clip(lower=0.0), 14)
        loss = _wilder(-delta.clip(upper=0.0), 14)
        rsi = 100 * gain / (gain + loss)
        # Perfectly flat market (gain+loss == 0) is neutral by definition.
        return rsi.fillna(50.0)
    if name in {"macd", "macd_signal", "macd_hist"}:
        macd_line = (
            close.ewm(span=12, adjust=False).mean()
            - close.ewm(span=26, adjust=False).mean()
        )
        signal = macd_line.ewm(span=9, adjust=False).mean()
        return {"macd": macd_line, "macd_signal": signal,
                "macd_hist": macd_line - signal}[name]
    if name == "boll_mid":
        return close.rolling(20).mean()
    if name in {"boll_upper", "boll_lower"}:
        mid = close.rolling(20).mean()
        # Bollinger convention: population standard deviation over the window.
        band = 2 * close.rolling(20).std(ddof=0)
        return mid + band if name == "boll_upper" else mid - band
    if name == "atr_14":
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        return _wilder(tr, 14)
    raise KeyError(f"unknown indicator: {name}")  # pragma: no cover - guarded


def compute_indicators(series: OhlcvSeries) -> TechnicalSnapshot:
    """Compute the fixed Phase 1 indicator set for one normalized series."""
    now = datetime.now(timezone.utc)
    base = TechnicalSnapshot(
        asset_id=series.asset_id,
        timeframe=series.timeframe,
        computed_at=now,
        bar_count=len(series.bars),
        first_bar_timestamp=series.bars[0].timestamp if series.bars else None,
        latest_close=series.latest_close,
        latest_bar_timestamp=series.latest_timestamp,
    )
    if len(series.bars) == 0:
        base.missing_reasons["*"] = "no bars in series"
        return base

    df = _series_frame(series)

    def value(name: str) -> tuple[float | None, str | None]:
        needed = _REQUIRED_LOOKBACK.get(name)
        if needed is not None and len(df) < needed:
            return None, f"insufficient lookback: {len(df)} < {needed} bars"
        raw = _clean(_indicator_series(name, df).iloc[-1])
        if raw is None:
            return None, "indicator produced no finite value on latest bar"
        return raw, None

    indicators: dict[str, float | None] = {}
    missing: dict[str, str] = {}
    for name in _REQUIRED_LOOKBACK:
        val, reason = value(name)
        indicators[name] = val
        if reason:
            missing[name] = reason

    # Price momentum: % change over the last N closed bars.
    if len(df) >= _MOMENTUM_LOOKBACK + 1:
        prev = float(df["Close"].iloc[-1 - _MOMENTUM_LOOKBACK])
        indicators["momentum_10_pct"] = (
            (float(df["Close"].iloc[-1]) - prev) / prev * 100
        )
    else:
        indicators["momentum_10_pct"] = None
        missing["momentum_10_pct"] = (
            f"insufficient lookback: {len(df)} bars < {_MOMENTUM_LOOKBACK + 1}"
        )

    # Realized volatility, annualized with a venue-appropriate convention.
    minutes_per_year = (
        _MINUTES_PER_YEAR_CRYPTO if _is_crypto_series(series)
        else _MINUTES_PER_YEAR_VENUE
    )
    vol, n_used = _realized_volatility(df.set_index("Date")["Close"], minutes_per_year)
    indicators["realized_volatility_annualized"] = vol
    if vol is None:
        missing["realized_volatility_annualized"] = (
            f"insufficient lookback: {n_used} bars < {_VOLATILITY_MIN_BARS} usable"
        )

    base.indicators = indicators
    base.missing_reasons = missing
    base.trend = classify_trend(
        base.latest_close,
        indicators.get("sma_50"),
        indicators.get("sma_200"),
    )
    base.momentum = classify_momentum(
        indicators.get("macd_hist"), indicators.get("rsi_14")
    )
    base.volatility = classify_volatility(indicators.get("atr_14"), base.latest_close)
    return base


def _is_crypto_series(series: OhlcvSeries) -> bool:
    from tradingagents.assets.registry import AssetClass, get_asset

    try:
        return get_asset(series.asset_id).asset_class is AssetClass.CRYPTO
    except KeyError:
        return False


def render_snapshot(snapshot: TechnicalSnapshot) -> str:
    """Render the snapshot as a deterministic markdown block for agent prompts."""
    lines = [
        f"## Verified technical snapshot — {snapshot.asset_id} "
        f"@ {snapshot.timeframe.value}",
        "",
        f"- Bars analyzed: {snapshot.bar_count} "
        f"(latest bar {snapshot.latest_bar_timestamp.isoformat() if snapshot.latest_bar_timestamp else 'n/a'})",
        f"- Latest close: {snapshot.latest_close}",
        f"- Deterministic classification: trend={snapshot.trend}, "
        f"momentum={snapshot.momentum}, volatility={snapshot.volatility}",
        "",
        "| Indicator | Value |",
        "|---|---:|",
    ]
    for name in sorted(snapshot.indicators):
        val = snapshot.indicators[name]
        shown = "unavailable" if val is None else f"{val:.4f}".rstrip("0").rstrip(".")
        note = ""
        if val is None and name in snapshot.missing_reasons:
            note = f" ({snapshot.missing_reasons[name]})"
        lines.append(f"| {name} | {shown}{note} |")
    lines += [
        "",
        "Treat these numbers as the source of truth for exact claims. Do not",
        "invent values for indicators marked unavailable.",
    ]
    return "\n".join(lines)
