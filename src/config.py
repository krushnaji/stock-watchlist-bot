"""Load and validate YAML configuration and watchlist."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Project root = parent of src/
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"
DEFAULT_WATCHLIST = ROOT / "watchlist.yaml"


@dataclass
class Stock:
    symbol: str
    name: str
    query: str
    sector: str

    @property
    def yahoo(self) -> str:
        """Yahoo Finance ticker with NSE suffix."""
        return f"{self.symbol}.NS"


@dataclass
class AlertFlags:
    enable_news: bool = True
    enable_price_move: bool = True
    enable_volume_spike: bool = True
    enable_near_52w: bool = True
    enable_deal_keywords: bool = True


@dataclass
class AppConfig:
    price_move_threshold_pct: float = 4.0
    re_alert_step_pct: float = 4.0
    volume_spike_ratio: float = 2.5
    near_52w_pct: float = 2.0
    deal_keywords: list[str] = field(default_factory=list)
    digest_times: list[str] = field(default_factory=list)
    market_start: str = "09:15"
    market_end: str = "15:30"
    market_tz: str = "Asia/Kolkata"
    index_symbol: str = "^NSEI"
    price_period: str = "1y"
    price_interval: str = "1d"
    news_lookback: str = "1d"
    max_headlines_per_stock: int = 2
    retention_days: int = 5
    http_timeout: int = 20
    http_retries: int = 3
    http_pause_seconds: float = 1.5
    alerts: AlertFlags = field(default_factory=AlertFlags)
    stocks: list[Stock] = field(default_factory=list)
    sectors: dict[str, list[Stock]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {path}, got {type(data)}")
    return data


def load_watchlist(path: Path | None = None) -> tuple[list[Stock], dict[str, list[Stock]]]:
    """Parse watchlist.yaml into flat stock list and sector -> stocks map."""
    path = path or DEFAULT_WATCHLIST
    data = _load_yaml(path)
    sectors_raw = data.get("sectors") or []
    stocks: list[Stock] = []
    by_sector: dict[str, list[Stock]] = {}

    for sector_block in sectors_raw:
        sector_name = str(sector_block.get("name", "Other")).strip()
        entries = sector_block.get("stocks") or []
        for entry in entries:
            stock = Stock(
                symbol=str(entry["symbol"]).strip(),
                name=str(entry.get("name") or entry["symbol"]).strip(),
                query=str(entry.get("query") or entry["symbol"]).strip(),
                sector=sector_name,
            )
            stocks.append(stock)
            by_sector.setdefault(sector_name, []).append(stock)

    logger.info("Loaded %d stocks across %d sectors from %s", len(stocks), len(by_sector), path)
    return stocks, by_sector


def load_config(
    config_path: Path | None = None,
    watchlist_path: Path | None = None,
) -> AppConfig:
    """Load config.yaml + watchlist.yaml into a single AppConfig."""
    config_path = config_path or DEFAULT_CONFIG
    raw = _load_yaml(config_path)
    stocks, by_sector = load_watchlist(watchlist_path)

    market = raw.get("market_hours") or {}
    news = raw.get("news") or {}
    http = raw.get("http") or {}
    alerts_raw = raw.get("alerts") or {}

    alerts = AlertFlags(
        enable_news=bool(alerts_raw.get("enable_news", True)),
        enable_price_move=bool(alerts_raw.get("enable_price_move", True)),
        enable_volume_spike=bool(alerts_raw.get("enable_volume_spike", True)),
        enable_near_52w=bool(alerts_raw.get("enable_near_52w", True)),
        enable_deal_keywords=bool(alerts_raw.get("enable_deal_keywords", True)),
    )

    cfg = AppConfig(
        price_move_threshold_pct=float(raw.get("price_move_threshold_pct", 4.0)),
        re_alert_step_pct=float(raw.get("re_alert_step_pct", 4.0)),
        volume_spike_ratio=float(raw.get("volume_spike_ratio", 2.5)),
        near_52w_pct=float(raw.get("near_52w_pct", 2.0)),
        deal_keywords=[str(k) for k in (raw.get("deal_keywords") or [])],
        digest_times=[str(t) for t in (raw.get("digest_times") or [])],
        market_start=str(market.get("start", "09:15")),
        market_end=str(market.get("end", "15:30")),
        market_tz=str(market.get("tz", "Asia/Kolkata")),
        index_symbol=str(raw.get("index_symbol", "^NSEI")),
        price_period=str(raw.get("price_period", "1y")),
        price_interval=str(raw.get("price_interval", "1d")),
        news_lookback=str(news.get("lookback", "1d")),
        max_headlines_per_stock=int(news.get("max_headlines_per_stock", 2)),
        retention_days=int(news.get("retention_days", 5)),
        http_timeout=int(http.get("timeout", 20)),
        http_retries=int(http.get("retries", 3)),
        http_pause_seconds=float(http.get("pause_seconds", 1.5)),
        alerts=alerts,
        stocks=stocks,
        sectors=by_sector,
        raw=raw,
    )
    return cfg
