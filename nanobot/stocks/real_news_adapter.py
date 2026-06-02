"""Real A-share stock news adapter backed by Eastmoney and Sina."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

import httpx

from nanobot.stocks.service import NewsArticle, NewsDataAdapter

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
_TAG_RE = re.compile(r"<[^>]+>")
_SINA_ENTRY_RE = re.compile(
    r"<div class=\"datelist\"><span>(?P<time>[^<]+)</span></div>\s*"
    r"<a href=\"(?P<url>[^\"]+)\"[^>]*>(?P<title>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class EastmoneySinaNewsAdapter(NewsDataAdapter):
    """Fetch stock-specific news from Eastmoney with Sina fallback."""

    timeout: float = 15.0
    current_date_provider: Callable[[], date | datetime | str] = field(
        default=lambda: date.today()
    )

    def get_news(
        self,
        symbol: str,
        lookback_days: int,
        anchor_date: date | str | None = None,
    ) -> list[NewsArticle]:
        start_date, end_date = self._date_window(lookback_days, anchor_date)
        eastmoney_error: Exception | None = None

        try:
            eastmoney_articles = self._fetch_news_eastmoney(symbol)
        except Exception as exc:
            eastmoney_error = exc
            eastmoney_articles = []

        eastmoney_articles = self._filter_by_date(eastmoney_articles, start_date, end_date)
        if eastmoney_articles:
            return eastmoney_articles

        try:
            sina_articles = self._fetch_news_sina(symbol)
        except Exception as exc:
            if eastmoney_error is not None:
                raise RuntimeError(
                    f"failed to fetch stock news for {symbol}: eastmoney={eastmoney_error}; sina={exc}"
                ) from exc
            raise RuntimeError(f"failed to fetch stock news for {symbol}: sina={exc}") from exc

        sina_articles = self._filter_by_date(sina_articles, start_date, end_date)
        if sina_articles:
            return sina_articles

        if eastmoney_error is not None:
            raise RuntimeError(f"failed to fetch stock news for {symbol}: eastmoney={eastmoney_error}")
        return []

    def _fetch_news_eastmoney(self, symbol: str) -> list[NewsArticle]:
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        inner_param = {
            "uid": "",
            "keyword": symbol,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": 20,
                    "preTag": "",
                    "postTag": "",
                }
            },
        }
        response = httpx.get(
            url,
            params={"cb": "jQuery_news", "param": json.dumps(inner_param, separators=(",", ":"))},
            headers={"User-Agent": _UA, "Referer": "https://so.eastmoney.com/"},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"eastmoney http {response.status_code}")
        text = response.text
        start = text.find("(")
        end = text.rfind(")")
        if start < 0 or end <= start:
            raise ValueError("invalid eastmoney jsonp payload")
        payload = json.loads(text[start + 1 : end])
        rows = payload.get("result", {}).get("cmsArticleWebOld", {}).get("list", [])

        return [
            NewsArticle(
                title=self._strip_html(row.get("title", "")),
                summary=self._strip_html(row.get("content", ""))[:200],
                published_at=self._normalize_time(row.get("date", "")),
                source=self._strip_html(row.get("mediaName", "")),
            )
            for row in rows
        ]

    def _fetch_news_sina(self, symbol: str) -> list[NewsArticle]:
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        url = (
            "https://vip.stock.finance.sina.com.cn/corp/view/"
            f"vCB_AllNewsStock.php?symbol={prefix}{symbol}&Page=1"
        )
        response = httpx.get(
            url,
            headers={"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"sina http {response.status_code}")
        rows: list[NewsArticle] = []
        for match in _SINA_ENTRY_RE.finditer(response.text):
            rows.append(
                NewsArticle(
                    title=self._strip_html(match.group("title")),
                    summary="",
                    published_at=self._normalize_sina_time(match.group("time")),
                    source="新浪财经",
                )
            )
        return rows

    def _filter_by_date(
        self,
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
    def _normalize_time(raw: str) -> str:
        raw = (raw or "").strip()
        for fmt_in, fmt_out in (
            ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"),
            ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M"),
            ("%Y-%m-%d", "%Y-%m-%d"),
        ):
            try:
                return datetime.strptime(raw, fmt_in).strftime(fmt_out)
            except ValueError:
                continue
        return raw

    @staticmethod
    def _normalize_sina_time(raw: str) -> str:
        raw = raw.strip()
        for fmt_in in ("%Y年%m月%d日 %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, fmt_in)
                if parsed.time() == datetime.min.time():
                    return parsed.strftime("%Y-%m-%d")
                return parsed.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                continue
        return raw

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
