from __future__ import annotations

import json

import httpx
import pytest

from nanobot.stocks.real_news_adapter import EastmoneySinaNewsAdapter


def _eastmoney_payload(*, title: str, content: str, date: str, source: str, url: str) -> str:
    body = {
        "result": {
            "cmsArticleWebOld": {
                "list": [
                    {
                        "title": title,
                        "content": content,
                        "date": date,
                        "mediaName": source,
                        "url": url,
                    }
                ]
            }
        }
    }
    return f"jQuery_news({json.dumps(body, ensure_ascii=False)})"


def test_real_news_adapter_parses_eastmoney_jsonp_and_filters_by_lookback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        del url, kwargs
        return httpx.Response(
            200,
            text=_eastmoney_payload(
                title="<em>公司</em>收到证监会立案告知书",
                content="公司披露涉嫌信息披露违法违规。",
                date="2026-05-30 09:30:00",
                source="东方财富网",
                url="https://example.com/article",
            ),
        )

    monkeypatch.setattr("nanobot.stocks.real_news_adapter.httpx.get", fake_get)

    adapter = EastmoneySinaNewsAdapter(current_date_provider=lambda: "2026-06-01")

    articles = adapter.get_news("600001", 7)

    assert len(articles) == 1
    assert articles[0].title == "公司收到证监会立案告知书"
    assert articles[0].summary == "公司披露涉嫌信息披露违法违规。"
    assert articles[0].published_at == "2026-05-30 09:30"
    assert articles[0].source == "东方财富网"


def test_real_news_adapter_uses_sina_fallback_when_eastmoney_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        calls.append(url)
        if "search-api-web.eastmoney.com" in url:
            raise httpx.HTTPError("eastmoney down")
        return httpx.Response(
            200,
            text="""
            <html><body>
              <div class="datelist"><span>2026年05月31日 10:00</span></div>
              <a href="https://finance.sina.com.cn/article" target="_blank">公司签订重大合同</a>
            </body></html>
            """,
        )

    monkeypatch.setattr("nanobot.stocks.real_news_adapter.httpx.get", fake_get)

    adapter = EastmoneySinaNewsAdapter(current_date_provider=lambda: "2026-06-01")

    articles = adapter.get_news("600001", 7)

    assert len(articles) == 1
    assert articles[0].title == "公司签订重大合同"
    assert articles[0].published_at == "2026-05-31 10:00"
    assert articles[0].source == "新浪财经"
    assert any("eastmoney.com" in url for url in calls)
    assert any("sina.com.cn" in url for url in calls)


def test_real_news_adapter_raises_when_all_sources_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        del kwargs
        raise httpx.HTTPError(f"failed: {url}")

    monkeypatch.setattr("nanobot.stocks.real_news_adapter.httpx.get", fake_get)

    adapter = EastmoneySinaNewsAdapter(current_date_provider=lambda: "2026-06-01")

    with pytest.raises(RuntimeError, match="failed to fetch stock news"):
        adapter.get_news("600001", 7)


def test_real_news_adapter_prefers_trade_date_for_lookback_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {
        "result": {
            "cmsArticleWebOld": {
                "list": [
                    {
                        "title": "旧闻",
                        "content": "旧闻摘要",
                        "date": "2026-05-30 09:30:00",
                        "mediaName": "东方财富网",
                        "url": "https://example.com/old",
                    },
                    {
                        "title": "新闻",
                        "content": "新闻摘要",
                        "date": "2026-06-04 09:30:00",
                        "mediaName": "东方财富网",
                        "url": "https://example.com/new",
                    },
                ]
            }
        }
    }

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        del url, kwargs
        return httpx.Response(
            200,
            text=f"jQuery_news({json.dumps(body, ensure_ascii=False)})",
        )

    monkeypatch.setattr("nanobot.stocks.real_news_adapter.httpx.get", fake_get)

    adapter = EastmoneySinaNewsAdapter(current_date_provider=lambda: "2026-06-05")

    articles = adapter.get_news("600001", 3, anchor_date="2026-06-01")

    assert [article.title for article in articles] == ["旧闻"]
