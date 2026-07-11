"""Near-real-time monitor: alert only on NEW conditions since last run."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.config import AppConfig
from src.news import NewsItem, fetch_all_news, headline_has_deal_keyword
from src.prices import PriceSnapshot, fetch_prices
from src.results import (
    enrich_result_item,
    format_result_alert,
    headline_is_result,
    headline_mentions_stock,
)
from src.state import (
    is_market_open,
    load_last_prices,
    load_seen_news,
    mark_news_seen,
    prune_seen_news,
    save_last_prices,
    save_seen_news,
    today_in_tz,
)
from src.telegram import escape_md

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    priority: int  # lower = higher priority (0 = deal/HIGH)
    symbol: str
    kind: str
    text: str


@dataclass
class MonitorResult:
    messages: list[str] = field(default_factory=list)
    state_changed: bool = False


def _ensure_price_state(state: dict, symbol: str) -> dict:
    entry = state.get(symbol)
    if not isinstance(entry, dict):
        entry = {}
        state[symbol] = entry
    return entry


def _reset_daily_flags_if_needed(entry: dict, today: str) -> None:
    """Clear once-per-day flags when the calendar day rolls over."""
    if entry.get("last_alert_date") != today:
        entry["last_alert_date"] = today
        entry["volume_alerted"] = False
        entry["near_high_alerted"] = False
        entry["near_low_alerted"] = False
        # Keep price_alerted_pct across the day for re_alert_step logic;
        # clear it on a new day so the first move can fire again.
        entry.pop("price_alerted_pct", None)


def _check_price_alerts(
    snap: PriceSnapshot,
    cfg: AppConfig,
    entry: dict,
    market_open: bool,
    alerts: list[Alert],
) -> bool:
    """Return True if state mutated."""
    if not market_open:
        return False
    changed = False
    pct = snap.day_change_pct

    # Price move
    if cfg.alerts.enable_price_move and pct is not None:
        thr = cfg.price_move_threshold_pct
        if abs(pct) >= thr:
            last = entry.get("price_alerted_pct")
            should = False
            if last is None:
                should = True
            else:
                try:
                    last_f = float(last)
                    if abs(pct - last_f) >= cfg.re_alert_step_pct:
                        should = True
                except (TypeError, ValueError):
                    should = True
            if should:
                direction = "up" if pct > 0 else "down"
                price_bit = (
                    f"\n  Price: {snap.current:,.2f}" if snap.current is not None else ""
                )
                alerts.append(
                    Alert(
                        priority=2,
                        symbol=snap.symbol,
                        kind="price",
                        text=(
                            f"⚡ *Price move* `{escape_md(snap.symbol)}` "
                            f"{escape_md(snap.name)}\n"
                            f"  Day change: *{pct:+.2f}%* ({direction})"
                            f"{price_bit}"
                        ),
                    )
                )
                entry["price_alerted_pct"] = pct
                changed = True

    # Volume spike (once per day)
    if (
        cfg.alerts.enable_volume_spike
        and snap.volume_ratio is not None
        and snap.volume_ratio >= cfg.volume_spike_ratio
        and not entry.get("volume_alerted")
    ):
        extra = ""
        if snap.current is not None and snap.day_change_pct is not None:
            extra = f"\n  Price: {snap.current:,.2f}  Day: {snap.day_change_pct:+.2f}%"
        alerts.append(
            Alert(
                priority=3,
                symbol=snap.symbol,
                kind="volume",
                text=(
                    f"🔊 *Volume spike* `{escape_md(snap.symbol)}` "
                    f"{escape_md(snap.name)}\n"
                    f"  Ratio: *{snap.volume_ratio:.1f}x* "
                    f"(threshold {cfg.volume_spike_ratio}x)"
                    f"{extra}"
                ),
            )
        )
        entry["volume_alerted"] = True
        changed = True

    # Near 52-week high/low (once each per day)
    if cfg.alerts.enable_near_52w:
        if (
            snap.pct_from_high is not None
            and snap.pct_from_high <= cfg.near_52w_pct
            and not entry.get("near_high_alerted")
        ):
            high_bit = (
                f"\n  {snap.pct_from_high:.2f}% below high ({snap.high_52w:,.2f})"
                if snap.high_52w is not None
                else f"\n  {snap.pct_from_high:.2f}% below 52w high"
            )
            alerts.append(
                Alert(
                    priority=4,
                    symbol=snap.symbol,
                    kind="near_52w_high",
                    text=(
                        f"📈 *Near 52w high* `{escape_md(snap.symbol)}` "
                        f"{escape_md(snap.name)}{high_bit}"
                    ),
                )
            )
            entry["near_high_alerted"] = True
            changed = True

        if (
            snap.pct_from_low is not None
            and snap.pct_from_low <= cfg.near_52w_pct
            and not entry.get("near_low_alerted")
        ):
            low_bit = (
                f"\n  {snap.pct_from_low:.2f}% above low ({snap.low_52w:,.2f})"
                if snap.low_52w is not None
                else f"\n  {snap.pct_from_low:.2f}% above 52w low"
            )
            alerts.append(
                Alert(
                    priority=4,
                    symbol=snap.symbol,
                    kind="near_52w_low",
                    text=(
                        f"📉 *Near 52w low* `{escape_md(snap.symbol)}` "
                        f"{escape_md(snap.name)}{low_bit}"
                    ),
                )
            )
            entry["near_low_alerted"] = True
            changed = True

    return changed


def _check_news_alerts(
    symbol: str,
    name: str,
    items: list[NewsItem],
    cfg: AppConfig,
    seen: dict,
    alerts: list[Alert],
    snap: PriceSnapshot | None = None,
) -> None:
    """Append NEW news/deal/result alerts; always mark evaluated links as seen."""
    if (
        not cfg.alerts.enable_news
        and not cfg.alerts.enable_deal_keywords
        and not cfg.alerts.enable_result_alerts
    ):
        return

    for item in items:
        if item.link in seen:
            continue

        # Always record as seen so a flood cannot repeat next run
        mark_news_seen(seen, item.link, symbol, item.title)

        # Drop Google-News noise that doesn't name this stock
        if not headline_mentions_stock(item.title, symbol, name):
            continue

        is_result = headline_is_result(item.title, cfg.result_keywords)
        is_deal = headline_has_deal_keyword(item.title, cfg.deal_keywords)

        if is_result and cfg.alerts.enable_result_alerts:
            try:
                brief = enrich_result_item(item, cfg, snap=snap)
                text = format_result_alert(symbol, name, item, brief)
            except Exception as exc:  # noqa: BLE001
                logger.error("Result enrich failed for %s: %s", symbol, exc)
                text = (
                    f"🚨 *HIGH PRIORITY — Results / Earnings*\n"
                    f"`{escape_md(symbol)}` {escape_md(name)}\n"
                    f"[{escape_md(item.title)}]({item.link})\n"
                    f"_{escape_md(item.source)}_\n"
                    f"_Summary unavailable — open the headline link._"
                )
            alerts.append(Alert(priority=-1, symbol=symbol, kind="result", text=text))
            continue

        if is_deal and cfg.alerts.enable_deal_keywords:
            alerts.append(
                Alert(
                    priority=0,
                    symbol=symbol,
                    kind="deal",
                    text=(
                        f"🚨 *HIGH PRIORITY — Deal keyword*\n"
                        f"`{escape_md(symbol)}` {escape_md(name)}\n"
                        f"[{escape_md(item.title)}]({item.link})\n"
                        f"_{escape_md(item.source)}_"
                    ),
                )
            )
        elif cfg.alerts.enable_news:
            alerts.append(
                Alert(
                    priority=1,
                    symbol=symbol,
                    kind="news",
                    text=(
                        f"📰 *New headline* `{escape_md(symbol)}` "
                        f"{escape_md(name)}\n"
                        f"[{escape_md(item.title)}]({item.link})\n"
                        f"_{escape_md(item.source)}_"
                    ),
                )
            )


def run_monitor(cfg: AppConfig, persist: bool = True) -> MonitorResult:
    """
    Evaluate alert types; persist state when persist=True.
    First empty seen_news run bootstraps (seeds links, no flood).
    """
    result = MonitorResult()
    seen = load_seen_news()
    price_state = load_last_prices()
    today = today_in_tz(cfg.market_tz)
    market_open = is_market_open(cfg.market_start, cfg.market_end, cfg.market_tz)
    logger.info("Market open (%s): %s", cfg.market_tz, market_open)

    prices = fetch_prices(cfg)
    news_map = fetch_all_news(cfg)

    bootstrapping = cfg.bootstrap_seen_on_empty and len(seen) == 0
    if bootstrapping:
        seeded = 0
        for stock in cfg.stocks:
            for item in news_map.get(stock.symbol) or []:
                if item.link not in seen:
                    mark_news_seen(seen, item.link, stock.symbol, item.title)
                    seeded += 1
        logger.info("Bootstrap: seeded %d news links (no alert flood)", seeded)
        if persist:
            save_seen_news(seen)
            save_last_prices(price_state)
            result.state_changed = True
        result.messages = [
            (
                "*✅ Monitor primed*\n"
                f"Seeded {seeded} existing headlines as seen.\n"
                "Next runs will alert only on *new* items "
                f"(max {cfg.max_alerts_per_run}/run).\n"
            )
        ]
        return result

    alerts: list[Alert] = []

    for stock in cfg.stocks:
        snap = prices.get(stock.symbol)
        entry = _ensure_price_state(price_state, stock.symbol)
        _reset_daily_flags_if_needed(entry, today)

        if snap and snap.current is not None:
            _check_price_alerts(snap, cfg, entry, market_open, alerts)

        items = news_map.get(stock.symbol) or []
        _check_news_alerts(
            stock.symbol,
            stock.name,
            items,
            cfg,
            seen,
            alerts,
            snap=snap,
        )

    seen = prune_seen_news(seen, cfg.retention_days)

    if persist:
        save_seen_news(seen)
        save_last_prices(price_state)
        result.state_changed = True

    if not alerts:
        logger.info("No new alerts this run")
        return result

    alerts.sort(key=lambda a: (a.priority, a.symbol))
    total = len(alerts)
    capped = alerts[: max(1, cfg.max_alerts_per_run)]
    if total > len(capped):
        logger.info(
            "Capping alerts %d → %d (max_alerts_per_run)",
            total,
            len(capped),
        )

    # One Telegram message per alert (smaller payloads, fewer 429 multi-part storms)
    messages: list[str] = []
    for a in capped:
        messages.append(f"*🔔 Watchlist Alert*\n\n{a.text}\n")
    if total > len(capped):
        messages.append(
            f"_...and {total - len(capped)} more new items were recorded as seen "
            f"without pinging to avoid spam._\n"
        )
    result.messages = messages
    logger.info("Built %d alerts (sending %d)", total, len(capped))
    return result
