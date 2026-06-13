from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict

from nanobot.stocks.real_news_adapter import EastmoneySinaNewsAdapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Diagnose EastmoneySinaNewsAdapter.get_news() for a single stock."
    )
    parser.add_argument("symbol", help="Stock symbol, e.g. 001330")
    parser.add_argument("--name", default=None, help="Stock name, e.g. 博纳影业")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=7,
        help="Lookback window used to map TimeRange.",
    )
    parser.add_argument(
        "--anchor-date",
        default=None,
        help="Optional anchor date passed into get_news(), e.g. 2026-05-29.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=3,
        help="Requested result count sent to the upstream API.",
    )
    parser.add_argument(
        "--debug-logging",
        action="store_true",
        help="Enable adapter-level debug logging.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug_logging else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    adapter = EastmoneySinaNewsAdapter(
        timeout=args.timeout,
        result_count=args.count,
        debug_logging=args.debug_logging,
    )

    expected_query = f"{args.name} {args.symbol}" if args.name else args.symbol
    expected_query = f"{expected_query} 新闻"

    print("=== Adapter Input ===")
    print(
        json.dumps(
            {
                "symbol": args.symbol,
                "name": args.name,
                "lookback_days": args.lookback_days,
                "anchor_date": args.anchor_date,
                "timeout": args.timeout,
                "count": args.count,
                "expected_query": expected_query,
                "expected_time_range": adapter._time_range_for_lookback(args.lookback_days),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    try:
        articles = adapter.get_news(
            args.symbol,
            args.lookback_days,
            anchor_date=args.anchor_date,
            name=args.name,
        )
    except Exception as exc:
        print("=== Adapter Error ===")
        print(f"{type(exc).__name__}: {exc}")
        return 1

    print("=== Adapter Output ===")
    print(
        json.dumps(
            {
                "article_count": len(articles),
                "articles": [asdict(article) for article in articles],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
