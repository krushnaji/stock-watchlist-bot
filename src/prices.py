"""Fetch NSE prices via yfinance (batch download + per-ticker fallback)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import yfinance as yf

from src.config import AppConfig, Stock

logger = logging.getLogger(__name__)


@dataclass
class PriceSnapshot:
    symbol: str
    yahoo: str
    name: str
    sector: str
    current: float | None = None
    previous_close: float | None = None
    day_change_pct: float | None = None
    last_volume: float | None = None
    avg_volume_20: float | None = None
    volume_ratio: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    pct_from_high: float | None = None
    pct_from_low: float | None = None

    @property
    def near_52w_high(self) -> bool:
        return self.pct_from_high is not None and self.pct_from_high <= 0  # filled by caller threshold

    def distance_from_high_pct(self) -> float | None:
        return self.pct_from_high

    def distance_from_low_pct(self) -> float | None:
        return self.pct_from_low


def _safe_float(val: Any) -> float | None:
    try:
        if val is None:
            return None
        f = float(val)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _derive_from_history(hist) -> dict[str, float | None]:
    """Derive metrics from a yfinance history DataFrame for one ticker."""
    empty: dict[str, float | None] = {
        "current": None,
        "previous_close": None,
        "day_change_pct": None,
        "last_volume": None,
        "avg_volume_20": None,
        "volume_ratio": None,
        "high_52w": None,
        "low_52w": None,
        "pct_from_high": None,
        "pct_from_low": None,
    }
    if hist is None or hist.empty:
        return empty

    closes = hist["Close"].dropna()
    volumes = hist["Volume"].dropna() if "Volume" in hist.columns else None

    if closes.empty:
        return empty

    current = _safe_float(closes.iloc[-1])
    previous = _safe_float(closes.iloc[-2]) if len(closes) >= 2 else None
    day_pct = None
    if current is not None and previous is not None and previous != 0:
        day_pct = ((current - previous) / previous) * 100.0

    last_vol = _safe_float(volumes.iloc[-1]) if volumes is not None and not volumes.empty else None
    avg_vol = None
    vol_ratio = None
    if volumes is not None and len(volumes) >= 2:
        window = volumes.iloc[-21:-1] if len(volumes) >= 21 else volumes.iloc[:-1]
        if not window.empty:
            avg_vol = _safe_float(window.mean())
            if last_vol is not None and avg_vol and avg_vol > 0:
                vol_ratio = last_vol / avg_vol

    high_52w = _safe_float(closes.max())
    low_52w = _safe_float(closes.min())
    pct_high = None
    pct_low = None
    if current is not None and high_52w and high_52w > 0:
        pct_high = ((high_52w - current) / high_52w) * 100.0
    if current is not None and low_52w and low_52w > 0:
        pct_low = ((current - low_52w) / low_52w) * 100.0

    return {
        "current": current,
        "previous_close": previous,
        "day_change_pct": day_pct,
        "last_volume": last_vol,
        "avg_volume_20": avg_vol,
        "volume_ratio": vol_ratio,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "pct_from_high": pct_high,
        "pct_from_low": pct_low,
    }


def _download_one(
    yahoo: str,
    period: str,
    interval: str,
    retries: int,
    pause: float,
) -> Any:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            ticker = yf.Ticker(yahoo)
            hist = ticker.history(period=period, interval=interval, auto_adjust=True)
            if hist is not None and not hist.empty:
                return hist
            logger.warning("Empty history for %s (attempt %d/%d)", yahoo, attempt, retries)
        except Exception as exc:  # noqa: BLE001 — continue on any fetch error
            last_err = exc
            logger.warning("Fetch failed for %s (attempt %d/%d): %s", yahoo, attempt, retries, exc)
        if attempt < retries:
            time.sleep(pause)
    if last_err:
        logger.error("Giving up on %s: %s", yahoo, last_err)
    return None


def fetch_prices(cfg: AppConfig, stocks: list[Stock] | None = None) -> dict[str, PriceSnapshot]:
    """
    Batch-download all watchlist tickers (+ index), with per-ticker fallback.
    Returns map of base symbol -> PriceSnapshot (index keyed by index_symbol).
    """
    stocks = stocks if stocks is not None else cfg.stocks
    period = cfg.price_period
    interval = cfg.price_interval
    retries = cfg.http_retries
    pause = cfg.http_pause_seconds

    yahoo_list = [s.yahoo for s in stocks]
    index_yahoo = cfg.index_symbol
    all_tickers = yahoo_list + [index_yahoo]

    logger.info("Batch downloading %d tickers (period=%s interval=%s)", len(all_tickers), period, interval)
    batch_data: dict[str, Any] = {}

    try:
        raw = yf.download(
            tickers=" ".join(all_tickers),
            period=period,
            interval=interval,
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
        # Multi-ticker: columns are MultiIndex (ticker, field)
        if hasattr(raw.columns, "levels") and len(getattr(raw.columns, "levels", [])) >= 2:
            for ysym in all_tickers:
                try:
                    if ysym in raw.columns.get_level_values(0):
                        sub = raw[ysym].dropna(how="all")
                        if not sub.empty:
                            batch_data[ysym] = sub
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Could not slice batch for %s: %s", ysym, exc)
        elif len(all_tickers) == 1:
            batch_data[all_tickers[0]] = raw
        else:
            # Single-level columns — unexpected for multi; treat as one series
            logger.warning("Unexpected batch column layout; falling back per ticker")
    except Exception as exc:  # noqa: BLE001
        logger.error("Batch download failed: %s — will fallback per ticker", exc)

    results: dict[str, PriceSnapshot] = {}

    for stock in stocks:
        hist = batch_data.get(stock.yahoo)
        if hist is None or (hasattr(hist, "empty") and hist.empty):
            logger.info("Fallback single download for %s", stock.yahoo)
            hist = _download_one(stock.yahoo, period, interval, retries, pause)

        metrics = _derive_from_history(hist)
        snap = PriceSnapshot(
            symbol=stock.symbol,
            yahoo=stock.yahoo,
            name=stock.name,
            sector=stock.sector,
            **metrics,
        )
        if snap.current is None:
            logger.warning("No price data for %s — skipping metrics", stock.symbol)
        results[stock.symbol] = snap

    # Index (NIFTY 50)
    idx_hist = batch_data.get(index_yahoo)
    if idx_hist is None or (hasattr(idx_hist, "empty") and idx_hist.empty):
        logger.info("Fallback single download for index %s", index_yahoo)
        idx_hist = _download_one(index_yahoo, period, interval, retries, pause)
    idx_metrics = _derive_from_history(idx_hist)
    results[index_yahoo] = PriceSnapshot(
        symbol=index_yahoo,
        yahoo=index_yahoo,
        name="NIFTY 50",
        sector="Index",
        **idx_metrics,
    )

    ok = sum(1 for s in stocks if results.get(s.symbol) and results[s.symbol].current is not None)
    logger.info("Price fetch complete: %d/%d stocks with data", ok, len(stocks))
    return results
