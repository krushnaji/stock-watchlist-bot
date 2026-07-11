"""Earnings / results alerts: detect, find PDF links, build neutral summaries."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from html import unescape
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests

from src.config import AppConfig
from src.news import NewsItem
from src.prices import PriceSnapshot
from src.telegram import escape_md
from src.screener import screener_md_link

logger = logging.getLogger(__name__)

# Patterns that strongly suggest a results / earnings headline.
# Keep these relatively tight — stock-name check is applied separately.
_RESULT_TITLE_PATTERNS = [
    re.compile(r"\bQ[1-4]\b.*\b(result|results|profit|revenue|earnings|PAT)\b", re.I),
    re.compile(r"\b(result|results|profit|revenue|earnings|PAT)\b.*\bQ[1-4]\b", re.I),
    re.compile(r"\bFY\s?\d{2,4}\b.*\b(result|results|profit|PAT|revenue)\b", re.I),
    re.compile(r"\b(quarterly|financial)\s+results?\b", re.I),
    re.compile(r"\bresults?\s+(announced|announce|declared|out|live)\b", re.I),
    re.compile(r"\b(net\s+profit|PAT)\b.*\b(crore|cr\.?|%|rises?|falls?|up|down)\b", re.I),
]

# Reject obvious unrelated market-wide / other-company chatter
_RESULT_NOISE = re.compile(
    r"\b(wall street|earnings calendar|earnings season|among \d+|sensex ends|"
    r"nifty it index|adr[s]?\b|gap-down|brokerages back|earnings week|"
    r"results next week|board to meet on this date)\b",
    re.I,
)

_PDF_HREF_RE = re.compile(
    r'href=["\']([^"\']+\.pdf[^"\']*)["\']',
    re.I,
)
_META_DESC_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']+)["\']',
    re.I,
)
_META_DESC_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\'](?:description|og:description)["\']',
    re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Pull numbers like +12%, Rs 1,234 crore, YoY from free text
_FACT_PATTERNS = [
    re.compile(
        r"(?:revenue|sales|turnover)[^\n.]{0,40}?(?:up|down|rose|fell|grew|growth)?"
        r"[^\n.]{0,20}?\d[\d,]*(?:\.\d+)?\s*(?:%|crore|cr\.?|billion)",
        re.I,
    ),
    re.compile(
        r"(?:PAT|net profit|profit)[^\n.]{0,40}?(?:up|down|rose|fell|grew|growth)?"
        r"[^\n.]{0,20}?\d[\d,]*(?:\.\d+)?\s*(?:%|crore|cr\.?|billion)",
        re.I,
    ),
    re.compile(r"(?:YoY|year[- ]on[- ]year|QoQ)[^\n.]{0,30}?\d[\d,]*(?:\.\d+)?\s*%", re.I),
    re.compile(r"\d[\d,]*(?:\.\d+)?\s*%\s*(?:YoY|QoQ|growth)", re.I),
]


@dataclass
class ResultBrief:
    """Neutral facts-only brief for a results headline (not investment advice)."""

    is_result: bool
    summary_lines: list[str]
    pdf_url: str | None = None
    article_url: str | None = None
    snippet: str | None = None


def headline_is_result(title: str, keywords: Iterable[str] | None = None) -> bool:
    """True if headline looks like earnings / quarterly results (not market chatter)."""
    if not title:
        return False
    if _RESULT_NOISE.search(title):
        return False
    for pat in _RESULT_TITLE_PATTERNS:
        if pat.search(title):
            return True
    if keywords:
        lower = title.lower()
        for kw in keywords:
            if kw and len(kw) >= 5 and kw.lower() in lower:
                return True
    return False


def headline_mentions_stock(title: str, symbol: str, name: str) -> bool:
    """Require the headline to mention this stock (cuts Google News false positives)."""
    if not title:
        return False
    t = title.lower()
    sym = (symbol or "").lower().strip()
    if sym and len(sym) >= 2 and re.search(rf"\b{re.escape(sym)}\b", t, re.I):
        return True
    # Name tokens (skip tiny words)
    for part in re.split(r"[\s&/,.\-]+", name or ""):
        token = part.strip().lower()
        if len(token) < 4:
            continue
        if token in {"limited", "india", "technologies", "industries", "motors", "power"}:
            continue
        if token in t:
            return True
    return False


def _http_get_bytes(
    url: str,
    timeout: int,
    retries: int,
    pause: float,
    *,
    stream_head: bool = False,
) -> tuple[str | None, bytes | None, str | None]:
    """
    GET url with redirects. Returns (final_url, body_bytes, content_type).
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; stock-watchlist-bot/1.0; +github-actions)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    }
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers=headers,
                allow_redirects=True,
                stream=stream_head,
            )
            resp.raise_for_status()
            final = str(resp.url)
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if stream_head:
                # Only need headers / small peek for PDF sniff
                chunk = next(resp.iter_content(8192), b"")
                resp.close()
                return final, chunk, ctype
            return final, resp.content, ctype
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning("HTTP GET failed (attempt %d/%d) %s: %s", attempt, retries, url[:80], exc)
            if attempt < retries:
                time.sleep(pause)
    logger.error("HTTP GET giving up %s: %s", url[:80], last_err)
    return None, None, None


def _strip_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _extract_meta_description(html: str) -> str | None:
    for pat in (_META_DESC_RE, _META_DESC_RE_ALT):
        m = pat.search(html)
        if m:
            return unescape(m.group(1)).strip()
    return None


def _extract_pdf_urls(html: str, base_url: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in _PDF_HREF_RE.finditer(html):
        raw = unescape(m.group(1).strip())
        if raw.startswith("//"):
            raw = "https:" + raw
        abs_url = urljoin(base_url, raw)
        if abs_url in seen:
            continue
        seen.add(abs_url)
        # Prefer corporate / exchange-looking PDFs
        found.append(abs_url)
    # Rank: bse/nse/sebi/investor/result in path first
    def score(u: str) -> int:
        low = u.lower()
        s = 0
        for token in ("bseindia", "nseindia", "sebi", "result", "earning", "investor", "financial"):
            if token in low:
                s += 2
        if low.endswith(".pdf"):
            s += 1
        return -s

    found.sort(key=score)
    return found


def _extract_facts(text: str, limit: int = 4) -> list[str]:
    facts: list[str] = []
    seen: set[str] = set()
    for pat in _FACT_PATTERNS:
        for m in pat.finditer(text):
            snippet = _WS_RE.sub(" ", m.group(0)).strip(" .;")
            key = snippet.lower()
            if key in seen or len(snippet) < 8:
                continue
            seen.add(key)
            facts.append(snippet)
            if len(facts) >= limit:
                return facts
    return facts


def enrich_result_item(
    item: NewsItem,
    cfg: AppConfig,
    snap: PriceSnapshot | None = None,
) -> ResultBrief:
    """
    Follow news link, try to find a PDF, and build a short neutral summary.
    Never emits buy/sell advice — facts + market context only.
    """
    summary: list[str] = []
    pdf_url: str | None = None
    article_url = item.link
    snippet: str | None = None

    final_url, body, ctype = _http_get_bytes(
        item.link,
        cfg.http_timeout,
        cfg.http_retries,
        cfg.http_pause_seconds,
    )
    if final_url:
        article_url = final_url

    html = ""
    if body and ctype and "pdf" in ctype:
        pdf_url = final_url or item.link
    elif body:
        try:
            html = body.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            html = ""

        # Direct PDF URL after redirect
        if final_url and final_url.lower().split("?")[0].endswith(".pdf"):
            pdf_url = final_url

        if not pdf_url and html:
            pdfs = _extract_pdf_urls(html, final_url or item.link)
            if pdfs:
                pdf_url = pdfs[0]

        snippet = _extract_meta_description(html) if html else None
        if not snippet and html:
            plain = _strip_html(html)
            snippet = plain[:400] if plain else None

    # --- Summary lines (neutral) ---
    summary.append(f"Headline: {item.title}")

    if snap and snap.day_change_pct is not None:
        price_bit = f"{snap.current:,.2f} " if snap.current is not None else ""
        summary.append(
            f"Market reaction (today): {price_bit}{snap.day_change_pct:+.2f}%"
        )
        if snap.volume_ratio is not None:
            summary.append(f"Volume vs 20d avg: {snap.volume_ratio:.1f}x")

    blob = " ".join(filter(None, [item.title, snippet or ""]))
    facts = _extract_facts(blob)
    if facts:
        summary.append("Numbers spotted in coverage:")
        for f in facts:
            summary.append(f"  • {f}")
    elif snippet:
        # One short context line from meta description
        short = snippet if len(snippet) <= 220 else snippet[:217] + "..."
        summary.append(f"Context: {short}")

    summary.append(
        "Note: Facts only — not buy/sell advice. Verify on exchange filings."
    )

    if pdf_url:
        # Quick HEAD/GET sniff — keep URL even if sniff fails
        logger.info("Result PDF candidate for %s: %s", item.stock_symbol, pdf_url)
    else:
        logger.info("No PDF found for result headline: %s", item.title[:80])

    return ResultBrief(
        is_result=True,
        summary_lines=summary,
        pdf_url=pdf_url,
        article_url=article_url,
        snippet=snippet,
    )


def format_result_alert(
    symbol: str,
    name: str,
    item: NewsItem,
    brief: ResultBrief,
) -> str:
    """Telegram Markdown (legacy) body for a results alert."""
    lines = [
        "🚨 *HIGH PRIORITY — Results / Earnings*",
        f"`{escape_md(symbol)}` {escape_md(name)} · {screener_md_link(symbol)}",
        f"[{escape_md(item.title)}]({item.link})",
        f"_{escape_md(item.source)}_",
        "",
        "*Summary*",
    ]
    for line in brief.summary_lines:
        lines.append(escape_md(line))

    if brief.pdf_url:
        lines.append("")
        lines.append(f"📄 [Result PDF / filing]({brief.pdf_url})")
    else:
        lines.append("")
        lines.append("_PDF not found in article — open headline link / BSE-NSE filings._")

    if brief.article_url and brief.article_url != item.link:
        # Avoid leaking ugly query-heavy URLs when identical intent
        host = urlparse(brief.article_url).netloc
        if host:
            lines.append(f"🔗 Resolved source host: `{escape_md(host)}`")

    return "\n".join(lines)
