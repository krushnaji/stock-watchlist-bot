"""Upcoming / mentioned quarterly result dates from free Google News headlines."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.config import AppConfig, Stock
from src.headlines import headline_mentions_stock, is_irrelevant_headline
from src.news import search_google_news
from src.telegram import escape_md

logger = logging.getLogger(__name__)

_CALENDAR_HINT = re.compile(
    r"\b("
    r"results?\s+date|board\s+meeting|to\s+announce|to\s+consider|"
    r"scheduled\s+to|earnings\s+on|results?\s+on|will\s+announce|"
    r"result\s+declaration|financial\s+results?\s+on|q[1-4]\s+results?"
    r")\b",
    re.I,
)

_MONTHS = (
    "january|february|march|april|may|june|july|august|september|"
    "october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)

# 15 July 2026 / July 15, 2026 / 15-Jul-2026 / 15/07/2026
_DATE_PATTERNS = [
    re.compile(
        rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?[\s\-/,]+(?P<mon>{_MONTHS})[\s\-/,]*(?P<y>\d{{4}})?\b",
        re.I,
    ),
    re.compile(
        rf"\b(?P<mon>{_MONTHS})[\s\-/]+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(?P<y>\d{{4}}))?\b",
        re.I,
    ),
    re.compile(
        r"\b(?P<d>\d{1,2})[/\-.](?P<m>\d{1,2})[/\-.](?P<y>\d{2,4})\b",
    ),
]

_MONTH_NUM = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


@dataclass
class EarningsHint:
    symbol: str
    name: str
    event_date: date | None
    label: str  # human text e.g. "15 Jul 2026 (est.)" or "this week (est.)"
    headline: str
    link: str
    source: str


def _parse_month_name(mon: str) -> int | None:
    return _MONTH_NUM.get(mon.lower().strip("."))


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def extract_date_from_text(text: str, today: date) -> date | None:
    """Best-effort date parse from a headline; None if none found."""
    if not text:
        return None
    lower = text.lower()
    if re.search(r"\btoday\b", lower):
        return today
    if re.search(r"\btomorrow\b", lower):
        return today + timedelta(days=1)

    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        gd = m.groupdict()
        try:
            day = int(gd.get("d") or 0)
            year_raw = gd.get("y")
            year = int(year_raw) if year_raw else today.year
            if year < 100:
                year += 2000
            if "mon" in gd and gd["mon"]:
                month = _parse_month_name(gd["mon"])
            else:
                month = int(gd.get("m") or 0)
            if not month or not day:
                continue
            dt = _safe_date(year, month, day)
            if dt is None:
                continue
            # If year omitted and date is > ~60 days in the past, assume next year
            if not year_raw and dt < today - timedelta(days=60):
                dt = _safe_date(today.year + 1, month, day) or dt
            return dt
        except (TypeError, ValueError):
            continue
    return None


def _is_calendar_headline(title: str) -> bool:
    return bool(_CALENDAR_HINT.search(title or ""))


def _calendar_query(stock: Stock) -> str:
    # Keep query tight — free Google News RSS
    return (
        f'("{stock.name}" OR {stock.symbol}) '
        f'(results date OR "board meeting" OR "to announce" OR "scheduled" OR earnings)'
    )


def fetch_earnings_hint(
    stock: Stock,
    cfg: AppConfig,
    today: date | None = None,
) -> EarningsHint | None:
    """
    Return one relevant upcoming/mentioned results-date hint for a stock, or None.
    Uses free Google News only; skips noise / unrelated headlines.
    """
    if not getattr(cfg, "earnings_calendar_enable", True):
        return None

    tz = ZoneInfo(cfg.market_tz)
    today = today or datetime.now(tz).date()
    lookback = cfg.earnings_lookback
    horizon = cfg.earnings_horizon_days
    past_grace = 3  # still show "results today/yesterday" briefly

    try:
        items = search_google_news(
            _calendar_query(stock),
            lookback,
            cfg,
            stock,
            apply_stock_filter=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Earnings calendar fetch failed for %s: %s", stock.symbol, exc)
        return None

    candidates: list[tuple[int, EarningsHint]] = []
    for item in items:
        if is_irrelevant_headline(item.title):
            continue
        if not headline_mentions_stock(item.title, stock.symbol, stock.name):
            continue
        # Must look like a results/board-meeting calendar story (not "stocks to watch today")
        if not _is_calendar_headline(item.title):
            continue

        event = extract_date_from_text(item.title, today)
        label: str
        score: int
        if event is not None:
            if event < today - timedelta(days=past_grace):
                continue
            if event > today + timedelta(days=horizon):
                continue
            label = event.strftime("%d %b %Y") + " (est.)"
            score = abs((event - today).days)
        elif re.search(r"\bthis week\b", item.title, re.I):
            label = "this week (est.)"
            score = 3
            event = today + timedelta(days=3)
        elif re.search(r"\bnext week\b", item.title, re.I):
            label = "next week (est.)"
            score = 7
            event = today + timedelta(days=7)
        else:
            # Calendar wording but no usable date — skip
            continue

        candidates.append(
            (
                score,
                EarningsHint(
                    symbol=stock.symbol,
                    name=stock.name,
                    event_date=event,
                    label=label,
                    headline=item.title,
                    link=item.link,
                    source=item.source,
                ),
            )
        )

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    best = candidates[0][1]
    logger.info("Earnings hint %s: %s", stock.symbol, best.label)
    return best


def fetch_all_earnings_hints(
    cfg: AppConfig,
    stocks: list[Stock] | None = None,
) -> dict[str, EarningsHint]:
    """Per-symbol hints; failures skipped. Only includes stocks with a relevant date."""
    if not cfg.earnings_calendar_enable:
        return {}
    stocks = stocks if stocks is not None else cfg.stocks
    out: dict[str, EarningsHint] = {}
    for stock in stocks:
        try:
            hint = fetch_earnings_hint(stock, cfg)
            if hint:
                out[stock.symbol] = hint
        except Exception as exc:  # noqa: BLE001
            logger.error("Earnings hint crashed for %s: %s", stock.symbol, exc)
        time.sleep(0.25)
    return out


def format_hint_line(hint: EarningsHint) -> str:
    """One Telegram Markdown line for digest."""
    title = escape_md(hint.headline)
    return (
        f"    📅 Results: *{escape_md(hint.label)}* — "
        f"[{title}]({hint.link})"
    )
