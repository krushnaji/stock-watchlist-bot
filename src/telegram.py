"""Telegram delivery layer (swappable channel interface)."""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Any, Sequence

import requests

logger = logging.getLogger(__name__)

# Telegram Markdown (legacy) hard limit is 4096; leave headroom for formatting
MAX_MESSAGE_CHARS = 4000


def escape_md(text: str) -> str:
    """
    Escape dynamic text for Telegram Markdown (legacy).
    Prevents stray _, *, `, [ from breaking formatting.
    """
    if text is None:
        return ""
    out = str(text)
    for ch in ("_", "*", "`", "["):
        out = out.replace(ch, f"\\{ch}")
    return out


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split on newline boundaries so chunks stay under `limit` chars."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


class DeliveryChannel(ABC):
    """Abstract delivery interface — implement WhatsApp later without touching digest/monitor."""

    @abstractmethod
    def send(self, text: str) -> bool:
        """Send a message. Return True on success."""


class DryRunChannel(DeliveryChannel):
    """Print messages instead of sending (for --dry-run)."""

    def send(self, text: str) -> bool:
        parts = split_message(text)
        for i, part in enumerate(parts, 1):
            print("=" * 60)
            if len(parts) > 1:
                print(f"[dry-run part {i}/{len(parts)}]")
            print(part)
            print("=" * 60)
        return True


class TelegramChannel(DeliveryChannel):
    """Telegram Bot API via requests (Markdown legacy + retries)."""

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        timeout: int = 20,
        retries: int = 5,
        pause_seconds: float = 1.5,
    ) -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.timeout = timeout
        self.retries = retries
        self.pause_seconds = pause_seconds

    def send(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
            return False

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        ok_all = True
        parts = split_message(text)
        for i, part in enumerate(parts):
            if not self._send_one(url, part):
                ok_all = False
            # Pace multi-part sends to reduce 429s
            if i < len(parts) - 1:
                time.sleep(max(self.pause_seconds, 1.0))
        return ok_all

    def _send_one(self, url: str, text: str) -> bool:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout)
                data: dict[str, Any] = {}
                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    data = {}

                if resp.status_code == 200 and data.get("ok"):
                    return True

                # Respect Telegram flood control
                if resp.status_code == 429:
                    retry_after = 5
                    params = data.get("parameters") or {}
                    try:
                        retry_after = int(params.get("retry_after") or retry_after)
                    except (TypeError, ValueError):
                        pass
                    wait = min(retry_after + 1, 90)
                    logger.warning(
                        "Telegram 429 — sleeping %ss (attempt %d/%d)",
                        wait,
                        attempt,
                        self.retries,
                    )
                    time.sleep(wait)
                    continue

                logger.warning(
                    "Telegram send failed (attempt %d/%d): %s %s",
                    attempt,
                    self.retries,
                    resp.status_code,
                    (resp.text or "")[:200],
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning("Telegram send error (attempt %d/%d): %s", attempt, self.retries, exc)
            if attempt < self.retries:
                time.sleep(self.pause_seconds)
        logger.error("Telegram send giving up: %s", last_err)
        return False


def get_channel(dry_run: bool, timeout: int = 20, retries: int = 5, pause: float = 1.5) -> DeliveryChannel:
    """Factory: dry-run printer or live Telegram."""
    if dry_run:
        return DryRunChannel()
    return TelegramChannel(timeout=timeout, retries=retries, pause_seconds=pause)


def send_messages(channel: DeliveryChannel, messages: Sequence[str], pause_seconds: float = 1.0) -> int:
    """Send a sequence of messages; return count of successes."""
    sent = 0
    for i, msg in enumerate(messages):
        if not msg or not msg.strip():
            continue
        try:
            if channel.send(msg):
                sent += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Delivery failed: %s", exc)
        if i < len(messages) - 1:
            time.sleep(max(pause_seconds, 0.5))
    return sent
