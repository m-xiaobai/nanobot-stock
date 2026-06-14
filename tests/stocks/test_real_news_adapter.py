from __future__ import annotations

import json
import logging

import pytest

from nanobot.stocks.real_news_adapter import EastmoneySinaNewsAdapter


def _event_payload(rows: list[dict[str, object]], *, summary: str = "") -> str:
    payload = {
        "Result": {
            "SearchContext": {
                "OriginQuery": "600001 舆情",
                "SearchType": "web-summary",
            },
            "WebResults": rows,
            "Choices": [
                {
                    "Delta": {
                        "Content": summary,
                    }
                }
            ]
            if summary
            else [],
        }
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}"


class _FakeStreamResponse:
    def __init__(self, status_code: int, lines: list[bytes], text: str = "") -> None:
        self.status_code = status_code
        self._lines = lines
        self.text = text

    def iter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamContext:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeStreamResponse:
        return self._response

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_real_news_adapter_maps_streamed_web_results_and_filters_by_lookback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "Title": "<em>公司</em>收到证监会立案告知书",
            "Snippet": "公司披露涉嫌信息披露违法违规。",
            "Summary": "综合摘要",
            "PublishTime": "2026-05-30 09:30:00",
            "SiteName": "东方财富网",
        }
    ]

    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamContext:
        assert method == "POST"
        assert url == "https://open.feedcoopapi.com/search_api/web_search"
        assert kwargs["headers"]["Authorization"] == "Bearer 54cfklYInzM5ZjqlvWcgviNcmB3swvkt"
        assert kwargs["json"]["Query"] == "600001 舆情"
        assert kwargs["json"]["SearchType"] == "web-summary"
        assert kwargs["json"]["NeedSummary"] is True
        assert kwargs["json"]["TimeRange"] == "OneWeek"
        return _FakeStreamContext(
            _FakeStreamResponse(
                200,
                [
                    _event_payload(rows, summary="第一段").encode("utf-8"),
                    b"data: [DONE]",
                ],
            )
        )

    monkeypatch.setattr("nanobot.stocks.real_news_adapter.httpx.stream", fake_stream)

    adapter = EastmoneySinaNewsAdapter(current_date_provider=lambda: "2026-06-01")

    articles = adapter.get_news("600001", 7)

    assert len(articles) == 1
    assert articles[0].title == "公司收到证监会立案告知书"
    assert articles[0].summary == "综合摘要"
    assert articles[0].published_at == "2026-05-30 09:30"
    assert articles[0].source == "东方财富网"


def test_real_news_adapter_raises_when_http_request_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamContext:
        del method, url, kwargs
        return _FakeStreamContext(_FakeStreamResponse(403, [], text="forbidden"))

    monkeypatch.setattr("nanobot.stocks.real_news_adapter.httpx.stream", fake_stream)

    adapter = EastmoneySinaNewsAdapter(current_date_provider=lambda: "2026-06-01")

    with pytest.raises(RuntimeError, match="feedcoop http 403"):
        adapter.get_news("600001", 7)


def test_real_news_adapter_returns_empty_when_payload_is_missing_web_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamContext:
        del method, url, kwargs
        return _FakeStreamContext(
            _FakeStreamResponse(
                200,
                [
                    b"data: {\"Result\": {}}",
                    b"data: [DONE]",
                ],
            )
        )

    monkeypatch.setattr("nanobot.stocks.real_news_adapter.httpx.stream", fake_stream)

    adapter = EastmoneySinaNewsAdapter(current_date_provider=lambda: "2026-06-01")

    assert adapter.get_news("600001", 7) == []


def test_real_news_adapter_prefers_trade_date_for_lookback_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "Title": "旧闻",
            "Snippet": "旧闻摘要",
            "PublishTime": "2026-05-30 09:30:00",
            "SiteName": "东方财富网",
        },
        {
            "Title": "新闻",
            "Snippet": "新闻摘要",
            "PublishTime": "2026-06-04 09:30:00",
            "SiteName": "东方财富网",
        },
    ]

    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamContext:
        del method, url, kwargs
        return _FakeStreamContext(
            _FakeStreamResponse(
                200,
                [
                    _event_payload(rows).encode("utf-8"),
                    b"data: [DONE]",
                ],
            )
        )

    monkeypatch.setattr("nanobot.stocks.real_news_adapter.httpx.stream", fake_stream)

    adapter = EastmoneySinaNewsAdapter(current_date_provider=lambda: "2026-06-05")

    articles = adapter.get_news("600001", 3, anchor_date="2026-06-01")

    assert [article.title for article in articles] == ["旧闻"]


def test_real_news_adapter_dedupes_and_sorts_newest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "Title": "较早新闻",
            "Snippet": "较早摘要",
            "PublishTime": "2026-05-28",
            "SiteName": "站点A",
        },
        {
            "Title": "较新新闻",
            "Snippet": "较新摘要",
            "PublishTime": "2026-05-29T09:30:00",
            "SiteName": "站点B",
        },
        {
            "Title": "较新新闻",
            "Snippet": "重复摘要",
            "PublishTime": "2026-05-29T09:30:00",
            "SiteName": "站点B",
        },
    ]

    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamContext:
        del method, url, kwargs
        return _FakeStreamContext(
            _FakeStreamResponse(
                200,
                [
                    _event_payload(rows).encode("utf-8"),
                    b"data: [DONE]",
                ],
            )
        )

    monkeypatch.setattr("nanobot.stocks.real_news_adapter.httpx.stream", fake_stream)

    adapter = EastmoneySinaNewsAdapter(current_date_provider=lambda: "2026-06-01")

    articles = adapter.get_news("600001", 7)

    assert [article.title for article in articles] == ["较新新闻", "较早新闻"]
    assert articles[0].published_at == "2026-05-29 09:30"


def test_real_news_adapter_accumulates_multiple_stream_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_rows = [
        {
            "Title": "第一批新闻",
            "Snippet": "第一批摘要",
            "PublishTime": "2026-05-29 09:30:00",
            "SiteName": "站点A",
            "AuthInfoLevel": 2,
        }
    ]
    second_rows = [
        {
            "Title": "第二批新闻",
            "Snippet": "第二批摘要",
            "PublishTime": "2026-05-29 10:30:00",
            "SiteName": "站点B",
            "AuthInfoLevel": 2,
        }
    ]

    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamContext:
        del method, url, kwargs
        return _FakeStreamContext(
            _FakeStreamResponse(
                200,
                [
                    _event_payload(first_rows).encode("utf-8"),
                    _event_payload(second_rows).encode("utf-8"),
                    b"data: [DONE]",
                ],
            )
        )

    monkeypatch.setattr("nanobot.stocks.real_news_adapter.httpx.stream", fake_stream)

    adapter = EastmoneySinaNewsAdapter(current_date_provider=lambda: "2026-06-01")

    articles = adapter.get_news("600001", 7)

    assert [article.title for article in articles] == ["第二批新闻", "第一批新闻"]


def test_real_news_adapter_preserves_unparseable_publish_time_but_filters_it_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "Title": "无法解析时间",
            "Snippet": "摘要",
            "PublishTime": "unknown-time",
            "SiteName": "站点A",
        }
    ]

    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamContext:
        del method, url, kwargs
        return _FakeStreamContext(
            _FakeStreamResponse(
                200,
                [
                    _event_payload(rows).encode("utf-8"),
                    b"data: [DONE]",
                ],
            )
        )

    monkeypatch.setattr("nanobot.stocks.real_news_adapter.httpx.stream", fake_stream)

    adapter = EastmoneySinaNewsAdapter(current_date_provider=lambda: "2026-06-01")

    assert adapter._normalize_time("unknown-time") == "unknown-time"
    assert adapter.get_news("600001", 7) == []


def test_real_news_adapter_filters_out_low_auth_info_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "Title": "低可信来源",
            "Snippet": "摘要A",
            "PublishTime": "2026-05-30 09:30:00",
            "SiteName": "站点A",
            "AuthInfoLevel": 1,
        },
        {
            "Title": "高可信来源",
            "Snippet": "摘要B",
            "PublishTime": "2026-05-30 10:00:00",
            "SiteName": "站点B",
            "AuthInfoLevel": 2,
        },
    ]

    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamContext:
        del method, url, kwargs
        return _FakeStreamContext(
            _FakeStreamResponse(
                200,
                [
                    _event_payload(rows).encode("utf-8"),
                    b"data: [DONE]",
                ],
            )
        )

    monkeypatch.setattr("nanobot.stocks.real_news_adapter.httpx.stream", fake_stream)

    adapter = EastmoneySinaNewsAdapter(current_date_provider=lambda: "2026-06-01")

    articles = adapter.get_news("600001", 7)

    assert [article.title for article in articles] == ["高可信来源"]


def test_real_news_adapter_emits_debug_logs_for_request_and_stream_events(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows = [
        {
            "Title": "高可信来源",
            "Snippet": "摘要B",
            "PublishTime": "2026-05-30 10:00:00",
            "SiteName": "站点B",
            "AuthInfoLevel": 2,
        }
    ]

    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamContext:
        del method, url, kwargs
        return _FakeStreamContext(
            _FakeStreamResponse(
                200,
                [
                    _event_payload(rows).encode("utf-8"),
                    b"data: [DONE]",
                ],
            )
        )

    monkeypatch.setattr("nanobot.stocks.real_news_adapter.httpx.stream", fake_stream)

    adapter = EastmoneySinaNewsAdapter(
        current_date_provider=lambda: "2026-06-01",
        debug_logging=True,
    )

    with caplog.at_level(logging.DEBUG, logger="nanobot.stocks.real_news_adapter"):
        adapter.get_news("001330", 7, name="博纳影业")

    messages = [record.getMessage() for record in caplog.records]
    assert any("query='博纳影业 001330 新闻'" in message for message in messages)
    assert any("time_range='OneWeek'" in message for message in messages)
    assert any("event_index=1" in message for message in messages)
    assert any("raw_results=1" in message for message in messages)
    assert any("filtered_results=1" in message for message in messages)
