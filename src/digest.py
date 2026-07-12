"""Build the twice-daily sector digest message(s)."""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.config import AppConfig
from src.earnings import fetch_all_earnings_hints, format_hint_line
from src.news import NewsItem, fetch_all_news
from src.prices import PriceSnapshot, fetch_prices
from src.screener import screener_md_link
from src.telegram import escape_md

logger = logging.getLogger(__name__)


def _fmt_price(p: float | None) -> str:
    if p is None:
        return "n/a"
    return f"{p:,.2f}"


def _fmt_pct(p: float | None) -> str:
    if p is None:
        return "n/a"
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.2f}%"


def _change_emoji(pct: float | None) -> str:
    if pct is None:
        return "➖"
    if pct > 0:
        return "🟢"
    if pct < 0:
        return "🔴"
    return "➖"


def _flag_line(snap: PriceSnapshot, cfg: AppConfig) -> str:
    flags: list[str] = []
    if snap.pct_from_high is not None and snap.pct_from_high <= cfg.near_52w_pct:
        flags.append(f"📈 near 52w high ({snap.pct_from_high:.1f}% away)")
    if snap.pct_from_low is not None and snap.pct_from_low <= cfg.near_52w_pct:
        flags.append(f"📉 near 52w low ({snap.pct_from_low:.1f}% away)")
    if snap.volume_ratio is not None and snap.volume_ratio >= cfg.volume_spike_ratio:
        flags.append(f"🔊 vol {snap.volume_ratio:.1f}x avg")
    return " · ".join(flags)


def _headline_links(items: list[NewsItem], limit: int) -> list[str]:
    lines: list[str] = []
    for item in items[:limit]:
        title = escape_md(item.title)
        # Markdown legacy link: [text](url) — escape title only; URL left raw
        lines.append(f"  • [{title}]({item.link})")
    return lines


def build_digest(cfg: AppConfig) -> list[str]:
    """
    Compose digest message(s):
      NIFTY line, Biggest Movers, Sector Scoreboard, then per-sector stocks + news.
    """
    prices = fetch_prices(cfg)
    news_map = fetch_all_news(cfg)
    earnings_map = fetch_all_earnings_hints(cfg)

    tz = ZoneInfo(cfg.market_tz)
    now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z")

    stock_snaps = [
        prices[s.symbol]
        for s in cfg.stocks
        if s.symbol in prices and prices[s.symbol].current is not None
    ]

    # --- Header + NIFTY ---
    lines: list[str] = [
        f"*📊 NSE Sector Digest*",
        f"_{escape_md(now_str)}_",
        "",
    ]

    nifty = prices.get(cfg.index_symbol)
    if nifty and nifty.current is not None:
        lines.append(
            f"*NIFTY 50:* {_fmt_price(nifty.current)}  "
            f"{_change_emoji(nifty.day_change_pct)} {_fmt_pct(nifty.day_change_pct)}"
        )
    else:
        lines.append("*NIFTY 50:* data unavailable")
    lines.append("")

    # --- Biggest Movers ---
    with_change = [s for s in stock_snaps if s.day_change_pct is not None]
    gainers = sorted(with_change, key=lambda s: s.day_change_pct or 0, reverse=True)[:3]
    losers = sorted(with_change, key=lambda s: s.day_change_pct or 0)[:3]

    lines.append("*🏆 Biggest Movers*")
    lines.append("_Gainers_")
    if gainers:
        for g in gainers:
            lines.append(
                f"  🟢 {escape_md(g.symbol)} {_fmt_pct(g.day_change_pct)} "
                f"({_fmt_price(g.current)})"
            )
    else:
        lines.append("  _n/a_")
    lines.append("_Losers_")
    if losers:
        for lo in losers:
            lines.append(
                f"  🔴 {escape_md(lo.symbol)} {_fmt_pct(lo.day_change_pct)} "
                f"({_fmt_price(lo.current)})"
            )
    else:
        lines.append("  _n/a_")
    lines.append("")

    # --- Sector Scoreboard ---
    sector_avgs: list[tuple[str, float]] = []
    for sector_name, sector_stocks in cfg.sectors.items():
        pcts = []
        for st in sector_stocks:
            snap = prices.get(st.symbol)
            if snap and snap.day_change_pct is not None:
                pcts.append(snap.day_change_pct)
        if pcts:
            sector_avgs.append((sector_name, sum(pcts) / len(pcts)))

    sector_avgs.sort(key=lambda x: x[1], reverse=True)
    lines.append("*📋 Sector Scoreboard*")
    for name, avg in sector_avgs:
        lines.append(f"  {_change_emoji(avg)} {escape_md(name)}: {_fmt_pct(avg)}")
    if not sector_avgs:
        lines.append("  _n/a_")
    lines.append("")

    # --- Upcoming results (only stocks with a free-source hint) ---
    if earnings_map:
        upcoming = sorted(
            earnings_map.values(),
            key=lambda h: h.event_date or date.max,
        )
        lines.append("*📅 Results calendar (est., from free news)*")
        for h in upcoming:
            lines.append(
                f"  • `{escape_md(h.symbol)}` {escape_md(h.label)}"
            )
        lines.append("")

    # --- Per sector ---
    for sector_name, sector_stocks in cfg.sectors.items():
        lines.append(f"*{escape_md(sector_name)}*")
        for st in sector_stocks:
            snap = prices.get(st.symbol)
            scr = screener_md_link(st.symbol)
            if not snap or snap.current is None:
                lines.append(f"  • {escape_md(st.symbol)} — _no data_ · {scr}")
            else:
                emoji = _change_emoji(snap.day_change_pct)
                line = (
                    f"  • *{escape_md(st.symbol)}* {_fmt_price(snap.current)} "
                    f"{emoji} {_fmt_pct(snap.day_change_pct)} · {scr}"
                )
                flag = _flag_line(snap, cfg)
                if flag:
                    line += f"\n    _{escape_md(flag)}_"
                lines.append(line)

            hint = earnings_map.get(st.symbol)
            if hint:
                lines.append(format_hint_line(hint))

            headlines = news_map.get(st.symbol) or []
            for hl in _headline_links(headlines, cfg.max_headlines_per_stock):
                lines.append(hl)
        lines.append("")

    body = "\n".join(lines).rstrip() + "\n"
    # telegram.split_message handles size; return as one logical message
    return [body]
