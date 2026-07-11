"""Screener.in helpers — free public company pages (link-only, no scraping)."""

from __future__ import annotations

from urllib.parse import quote

SCREENER_COMPANY = "https://www.screener.in/company/{symbol}/"


def screener_url(symbol: str) -> str:
    """Public Screener.in company page for an NSE symbol (e.g. NETWEB)."""
    return SCREENER_COMPANY.format(symbol=quote(str(symbol).strip().upper(), safe=""))


def screener_md_link(symbol: str, label: str = "Screener") -> str:
    """Telegram Markdown (legacy) link to Screener.in."""
    return f"[{label}]({screener_url(symbol)})"
