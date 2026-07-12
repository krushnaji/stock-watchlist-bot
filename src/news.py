"""Google News RSS fetch + parse (free, no API key)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus

import feedparser
import requests

from src.config import AppConfig, Stock
from src.headlines import headline_mentions_stock, is_irrelevant_headline

logger = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={query}+when:{lookback}&hl=en-IN&gl=IN&ceid=IN:en"
)


@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    published: datetime | None
    stock_symbol: str
    stock_name: str

    @property
    def published_iso(self) -> str | None:
        if self.published is None:
            return None
        return self.published.astimezone(timezone.utc).isoformat()


def _parse_published(entry: dict[str, Any]) -> datetime | None:
    for key in ("published", "updated"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (TypeError, ValueError, IndexError):
            continue
    # feedparser struct_time fallback
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _http_get(url: str, timeout: int, retries: int, pause: float) -> str | None:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "stock-watchlist-bot/1.0 (+github-actions)"},
            )
            resp.raise_for_status()
            return resp.text
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning("RSS GET failed (attempt %d/%d): %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(pause)
    logger.error("RSS GET giving up: %s — %s", url[:80], last_err)
    return None


def search_google_news(
    query: str,
    lookback: str,
    cfg: AppConfig,
    stock: Stock,
    *,
    apply_stock_filter: bool = True,
) -> list[NewsItem]:
    """
    Fetch Google News RSS for an arbitrary query, attached to `stock`.
    When apply_stock_filter=True, drop sports noise and non-matching headlines.
    """
    url = GOOGLE_NEWS_RSS.format(
        query=quote_plus(query),
        lookback=lookback,
    )
    body = _http_get(url, cfg.http_timeout, cfg.http_retries, cfg.http_pause_seconds)
    if not body:
        return []

    try:
        feed = feedparser.parse(body)
    except Exception as exc:  # noqa: BLE001
        logger.error("feedparser failed for query %s: %s", query[:60], exc)
        return []

    items: list[NewsItem] = []
    seen_links: set[str] = set()

    for entry in feed.entries or []:
        link = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        if link in seen_links:
            continue
        seen_links.add(link)

        source = ""
        src = entry.get("source")
        if isinstance(src, dict):
            source = (src.get("title") or "").strip()
        elif isinstance(src, str):
            source = src.strip()
        if not source:
            source = (entry.get("author") or "").strip() or "Google News"

        items.append(
            NewsItem(
                title=title,
                link=link,
                source=source,
                published=_parse_published(entry),
                stock_symbol=stock.symbol,
                stock_name=stock.name,
            )
        )

    items.sort(key=lambda n: n.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    if not apply_stock_filter:
        return items

    filtered: list[NewsItem] = []
    for item in items:
        if is_irrelevant_headline(item.title):
            continue
        if not headline_mentions_stock(item.title, stock.symbol, stock.name):
            continue
        filtered.append(item)
    return filtered


def fetch_news_for_stock(stock: Stock, cfg: AppConfig) -> list[NewsItem]:
    """Fetch and parse Google News RSS for one stock query."""
    items = search_google_news(
        stock.query,
        cfg.news_lookback,
        cfg,
        stock,
        apply_stock_filter=True,
    )
    return items


def fetch_all_news(cfg: AppConfig, stocks: list[Stock] | None = None) -> dict[str, list[NewsItem]]:
    """Fetch news for every stock; failures are logged and skipped."""
    stocks = stocks if stocks is not None else cfg.stocks
    out: dict[str, list[NewsItem]] = {}
    for stock in stocks:
        try:
            items = fetch_news_for_stock(stock, cfg)
            out[stock.symbol] = items
            logger.info("News for %s: %d items", stock.symbol, len(items))
        except Exception as exc:  # noqa: BLE001
            logger.error("News fetch crashed for %s: %s", stock.symbol, exc)
            out[stock.symbol] = []
        time.sleep(0.2)  # light politeness between RSS calls
    return out


def headline_has_deal_keyword(title: str, keywords: list[str]) -> bool:
    """Case-insensitive keyword match against a headline."""
    lower = title.lower()
    for kw in keywords:
        if kw.lower() in lower:
            return True
    return False
