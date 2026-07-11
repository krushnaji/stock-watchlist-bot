"""Persist seen-news and last-price alert state under state/."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import ROOT

logger = logging.getLogger(__name__)

STATE_DIR = ROOT / "state"
SEEN_NEWS_PATH = STATE_DIR / "seen_news.json"
LAST_PRICES_PATH = STATE_DIR / "last_prices.json"


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
        logger.warning("%s is not a JSON object — resetting", path)
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read %s: %s — starting fresh", path, exc)
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_state_dir()
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)
    logger.info("Wrote state: %s", path)


def load_seen_news(path: Path | None = None) -> dict[str, Any]:
    """
    seen_news.json shape:
      { "<link>": {"symbol": "...", "title": "...", "seen_at": "ISO8601"}, ... }
    """
    return _read_json(path or SEEN_NEWS_PATH)


def save_seen_news(data: dict[str, Any], path: Path | None = None) -> None:
    _write_json(path or SEEN_NEWS_PATH, data)


def load_last_prices(path: Path | None = None) -> dict[str, Any]:
    """
    last_prices.json shape per symbol:
      {
        "SYMBOL": {
          "last_alert_date": "YYYY-MM-DD",          # Asia/Kolkata date
          "price_alerted_pct": 5.2,                 # day % when last price alert fired
          "volume_alerted": true,
          "near_high_alerted": true,
          "near_low_alerted": true
        }
      }
    """
    return _read_json(path or LAST_PRICES_PATH)


def save_last_prices(data: dict[str, Any], path: Path | None = None) -> None:
    _write_json(path or LAST_PRICES_PATH, data)


def prune_seen_news(seen: dict[str, Any], retention_days: int) -> dict[str, Any]:
    """Drop seen-news entries older than retention_days."""
    if retention_days <= 0:
        return seen
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    kept: dict[str, Any] = {}
    dropped = 0
    for link, meta in seen.items():
        seen_at_raw = None
        if isinstance(meta, dict):
            seen_at_raw = meta.get("seen_at")
        elif isinstance(meta, str):
            seen_at_raw = meta
        if not seen_at_raw:
            kept[link] = meta
            continue
        try:
            seen_at = datetime.fromisoformat(str(seen_at_raw).replace("Z", "+00:00"))
            if seen_at.tzinfo is None:
                seen_at = seen_at.replace(tzinfo=timezone.utc)
            if seen_at >= cutoff:
                kept[link] = meta
            else:
                dropped += 1
        except (TypeError, ValueError):
            kept[link] = meta
    if dropped:
        logger.info("Pruned %d seen-news entries older than %d days", dropped, retention_days)
    return kept


def mark_news_seen(
    seen: dict[str, Any],
    link: str,
    symbol: str,
    title: str,
) -> None:
    seen[link] = {
        "symbol": symbol,
        "title": title,
        "seen_at": datetime.now(timezone.utc).isoformat(),
    }


def today_in_tz(tz_name: str) -> str:
    """Return YYYY-MM-DD for the given IANA timezone."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def is_market_open(cfg_start: str, cfg_end: str, tz_name: str) -> bool:
    """True if current local time is within [start, end] on a weekday (Mon–Fri)."""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(tz_name))
    if now.weekday() >= 5:  # Sat/Sun
        return False

    def _parse_hm(s: str) -> tuple[int, int]:
        parts = s.strip().split(":")
        return int(parts[0]), int(parts[1])

    sh, sm = _parse_hm(cfg_start)
    eh, em = _parse_hm(cfg_end)
    start_mins = sh * 60 + sm
    end_mins = eh * 60 + em
    now_mins = now.hour * 60 + now.minute
    return start_mins <= now_mins <= end_mins
