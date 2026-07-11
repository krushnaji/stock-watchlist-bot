"""CLI entrypoint: digest | monitor, with optional --dry-run."""

from __future__ import annotations

import argparse
import logging
import sys

from src.config import load_config
from src.digest import build_digest
from src.monitor import run_monitor
from src.telegram import get_channel, send_messages


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="NSE sector watchlist bot — Telegram digests & alerts",
    )
    parser.add_argument(
        "--mode",
        choices=("digest", "monitor"),
        required=True,
        help="digest = 2x/day summary; monitor = near-real-time alerts",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print messages instead of sending to Telegram; skip state writes for monitor",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    log = logging.getLogger("main")

    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        logging.error("Failed to load config: %s", exc)
        return 1

    channel = get_channel(
        dry_run=args.dry_run,
        timeout=cfg.http_timeout,
        retries=cfg.http_retries,
        pause=cfg.http_pause_seconds,
    )

    try:
        if args.mode == "digest":
            log.info("Building digest (dry_run=%s)", args.dry_run)
            messages = build_digest(cfg)
            sent = send_messages(channel, messages)
            log.info("Digest done — %d message(s) delivered", sent)
            return 0 if sent else 1

        # monitor
        log.info("Running monitor (dry_run=%s)", args.dry_run)
        # Digest is read-only; monitor writes state unless dry-run
        result = run_monitor(cfg, persist=not args.dry_run)
        if not result.messages:
            log.info("No alerts to send")
            return 0
        sent = send_messages(channel, result.messages, pause_seconds=cfg.http_pause_seconds)
        log.info("Monitor done — %d/%d message(s), state_changed=%s", sent, len(result.messages), result.state_changed)
        # Partial success is OK (rate limits); state already persisted to avoid re-flood
        return 0 if sent > 0 or not result.messages else 1

    except Exception as exc:  # noqa: BLE001
        log.exception("Run failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
