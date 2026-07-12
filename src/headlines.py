"""Headline relevance helpers shared by news / monitor / results."""

from __future__ import annotations

import re

# Tickers that are also common English words — match uppercase ticker or company name only
_COMMON_WORD_TICKERS = {
    "IDEA",
    "GAIN",
    "LAND",
    "BANK",
    "POWER",
    "TECH",
    "AUTO",
    "HOME",
    "CARE",
}

_NAME_SKIP = {
    "limited",
    "india",
    "technologies",
    "industries",
    "motors",
    "power",
    "travel",
    "analytics",
    "and",
    "the",
    "ltd",
    "beverages",
    "coffee",
    "space",
    "allied",
}

_IRRELEVANT = re.compile(
    r"\b("
    r"T20I?|IPL|cricket|football|soccer|tennis|badminton|"
    r"premier league|la liga|nba|nhl|world cup|asian cup|"
    r"test match|\bODI\b|live streaming|live telecast|"
    r"how to watch|vs eng|vs aus|vs pak|vs nz|ind vs|"
    r"movie review|box office|netflix show|spotify"
    r")\b",
    re.I,
)


def headline_mentions_stock(title: str, symbol: str, name: str) -> bool:
    """
    Require the headline to mention this stock (cuts Google News false positives).
    Short / common-word tickers (e.g. IDEA) are matched case-sensitively so English
    words like "idea" do not count.
    """
    if not title:
        return False
    t = title.lower()
    sym = (symbol or "").strip()
    sym_l = sym.lower()

    if sym and len(sym) >= 2:
        if sym.upper() in _COMMON_WORD_TICKERS or len(sym) <= 4:
            if re.search(rf"\b{re.escape(sym.upper())}\b", title):
                return True
        elif re.search(rf"\b{re.escape(sym_l)}\b", t):
            return True

    for part in re.split(r"[\s&/,.\-]+", name or ""):
        token = part.strip().lower()
        if len(token) < 4 or token in _NAME_SKIP:
            continue
        # Avoid "Idea" name token matching English "idea" for ticker IDEA
        if token == sym_l:
            continue
        if token in t:
            return True
    return False


def is_irrelevant_headline(title: str) -> bool:
    """Drop sports / entertainment noise that Google News often mixes in."""
    if not title:
        return True
    return bool(_IRRELEVANT.search(title))
