"""Real A-share stock news adapter backed by Feedcoop/Volc web search."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import logging
import re
from typing import Callable

import httpx

from nanobot.stocks.service import NewsArticle, NewsDataAdapter

_TAG_RE = re.compile(r"<[^>]+>")
_SEARCH_URL = "https://open.feedcoopapi.com/search_api/web_search"
_API_KEY = "BfbCJcg4wSObdCMEihGvKovrUjbZNmAF"
logger = logging.getLogger(__name__)


@dataclass
class EastmoneySinaNewsAdapter(NewsDataAdapter):
    """Fetch stock-specific news from Feedcoop/Volc web search."""

    timeout: float = 15.0
    result_count: int = 5
    debug_logging: bool = False
    current_date_provider: Callable[[], date | datetime | str] = field(
        default=lambda: date.today()
    )

    def get_news(
        self,
        symbol: str,
        lookback_days: int,
        anchor_date: date | str | None = None,
        name: str | None = None,
    ) -> list[NewsArticle]:
        # start_date, end_date = self._date_window(lookback_days, anchor_date)
        articles = self._fetch_news_websearch(symbol, lookback_days, name=name)
        # articles = self._filter_by_date(articles, start_date, end_date)
        return self._dedupe_and_sort(articles)

    def _fetch_news_websearch(
        self,
        symbol: str,
        lookback_days: int,
        *,
        name: str | None = None,
    ) -> list[NewsArticle]:
        raw_articles = self._stream_web_summary(symbol, lookback_days, name=name)
        return [
            NewsArticle(
                title=self._strip_html(str(row.get("Title") or "")),
                summary=self._strip_html(str(row.get("Summary") or row.get("Snippet") or "")),
                published_at=self._normalize_time(str(row.get("PublishTime") or "")),
                source=self._strip_html(str(row.get("SiteName") or "")),
            )
            for row in raw_articles
            if self._strip_html(str(row.get("Title") or "")).strip()
        ]

    def _stream_web_summary(
        self,
        symbol: str,
        lookback_days: int,
        *,
        name: str | None = None,
    ) -> list[dict[str, object]]:
        web_results: list[dict[str, object]] = []
        query = f"{name} {symbol}" if name else symbol
        request_query = f"{query} 新闻"
        time_range = self._time_range_for_lookback(lookback_days)
        if self.debug_logging:
            logger.debug(
                "feedcoop news request symbol=%s name=%r query=%r time_range=%r count=%s",
                symbol,
                name,
                request_query,
                time_range,
                self.result_count,
            )
        with httpx.stream(
            "POST",
            _SEARCH_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_API_KEY}",
            },
            json={
                "Query": request_query,
                "SearchType": "web_summary",
                "Count": self.result_count,
                "Filter": {
                    "NeedContent": False,
                    "AuthInfoLevel": 2,
                    "NeedUrl": True
                },
                "TimeRange": time_range,
                "Industry": "finance",
                
                "NeedSummary": True
            },
            timeout=self.timeout,
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(f"feedcoop http {response.status_code}: {response.text}")
            for event_index, raw_line in enumerate(response.iter_lines(), start=1):
                if not raw_line:
                    continue
                if isinstance(raw_line, bytes):
                    try:
                        line = raw_line.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                else:
                    line = str(raw_line).strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                result = event.get("Result") or {}
                current_results = result.get("WebResults")
                if isinstance(current_results, list) and current_results:
                    filtered_results = [
                        row
                        for row in current_results
                        if self._coerce_auth_level(row.get("AuthInfoLevel")) <= 2
                    ]
                    web_results = filtered_results
                    if self.debug_logging:
                        logger.debug(
                            "feedcoop news event symbol=%s event_index=%s raw_results=%s filtered_results=%s",
                            symbol,
                            event_index,
                            len(current_results),
                            len(filtered_results),
                        )
                elif self.debug_logging:
                    logger.debug(
                        "feedcoop news event symbol=%s event_index=%s raw_results=0 filtered_results=0",
                        symbol,
                        event_index,
                    )
        if not web_results:
            if self.debug_logging:
                logger.debug("feedcoop news result symbol=%s final_results=0", symbol)
            return []
        if self.debug_logging:
            logger.debug("feedcoop news result symbol=%s final_results=%s", symbol, len(web_results))
        return web_results

    @staticmethod
    def _coerce_auth_level(value: object) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

        self,
    def _filter_by_date(
        articles: list[NewsArticle],
        start_date: date,
        end_date: date,
    ) -> list[NewsArticle]:
        filtered: list[NewsArticle] = []
        for article in articles:
            parsed = self._parse_article_date(article.published_at)
            if parsed is None:
                continue
            if start_date <= parsed <= end_date:
                filtered.append(article)
        return filtered

    def _dedupe_and_sort(self, articles: list[NewsArticle]) -> list[NewsArticle]:
        deduped: list[NewsArticle] = []
        seen: set[tuple[str, str, str]] = set()
        for article in articles:
            key = (article.title, article.published_at, article.source)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(article)
        return sorted(deduped, key=self._sort_key, reverse=True)

    def _date_window(self, lookback_days: int, anchor_date: date | str | None = None) -> tuple[date, date]:
        end_date = self._coerce_date(anchor_date)
        if end_date is None:
            end_date = self._coerce_date(self.current_date_provider())
        if end_date is None:
            raise ValueError("unable to resolve news date window anchor")
        return end_date - timedelta(days=lookback_days), end_date

    @staticmethod
    def _strip_html(text: str) -> str:
        return _TAG_RE.sub("", text or "").strip()

    @staticmethod
    def _time_range_for_lookback(lookback_days: int) -> str:
        if lookback_days <= 1:
            return "OneDay"
        if lookback_days <= 7:
            return "OneWeek"
        if lookback_days <= 30:
            return "OneMonth"
        if lookback_days <= 90:
            return "ThreeMonths"
        return "OneYear"

    @staticmethod
    def _normalize_time(raw: str) -> str:
        raw = (raw or "").strip()
        for fmt_in, fmt_out in (
            ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"),
            ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M"),
            ("%Y-%m-%d", "%Y-%m-%d"),
            ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"),
            ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"),
        ):
            try:
                return datetime.strptime(raw, fmt_in).strftime(fmt_out)
            except ValueError:
                continue
        iso_raw = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso_raw)
        except ValueError:
            return raw
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        if parsed.time() == datetime.min.time():
            return parsed.strftime("%Y-%m-%d")
        return parsed.strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _parse_article_date(raw: str) -> date | None:
        raw = (raw or "").strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _sort_key(article: NewsArticle) -> datetime:
        raw = (article.published_at or "").strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return datetime.min

    @staticmethod
    def _coerce_date(value: date | datetime | str | None) -> date | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        raw = value.strip()
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None
