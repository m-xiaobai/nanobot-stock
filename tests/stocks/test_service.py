from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from nanobot.stocks.service import (
    DailySelectionService,
    DailySelectionServiceError,
    NewsArticle,
    NewsFilteredStock,
    PriceBar,
    PriceSeriesResponse,
    ScoredStock,
    ScreeningResult,
    StockUniverseResponse,
)


class _FakeMarketDataAdapter:
    def __init__(self, universe: list[str], price_map: dict[str, list[PriceBar]]) -> None:
        self._universe = universe
        self._price_map = price_map

    def get_stock_universe(self, market: str) -> StockUniverseResponse:
        return StockUniverseResponse(market=market, symbols=list(self._universe))

    def get_price_series(self, symbol: str, lookback_days: int) -> PriceSeriesResponse:
        return PriceSeriesResponse(symbol=symbol, bars=list(self._price_map[symbol]))


class _FakeNewsAdapter:
    def __init__(self, articles_by_symbol: dict[str, list[NewsArticle] | Exception]) -> None:
        self._articles_by_symbol = articles_by_symbol
        self.calls: list[tuple[str, int, object | None]] = []

    def get_news(self, symbol: str, lookback_days: int, anchor_date: object | None = None) -> list[NewsArticle]:
        self.calls.append((symbol, lookback_days, anchor_date))
        result = self._articles_by_symbol.get(symbol, [])
        if isinstance(result, Exception):
            raise result
        return list(result)


def _bar(
    day: int,
    *,
    open_price: float,
    close: float,
    high: float,
    low: float,
    volume: float,
) -> PriceBar:
    return PriceBar(
        trade_date=date(2026, 5, day),
        open=open_price,
        close=close,
        high=high,
        low=low,
        volume=volume,
    )


def _service(
    *,
    universe: list[str],
    price_map: dict[str, list[PriceBar]],
    articles_by_symbol: dict[str, list[NewsArticle] | Exception],
) -> DailySelectionService:
    return DailySelectionService(
        market_data=_FakeMarketDataAdapter(universe=universe, price_map=price_map),
        news_data=_FakeNewsAdapter(articles_by_symbol=articles_by_symbol),
    )


def _service_with_news_adapter(
    *,
    universe: list[str],
    price_map: dict[str, list[PriceBar]],
    news_data: _FakeNewsAdapter,
) -> DailySelectionService:
    return DailySelectionService(
        market_data=_FakeMarketDataAdapter(universe=universe, price_map=price_map),
        news_data=news_data,
    )


def test_screen_stocks_b1_strategy_is_deterministic() -> None:
    service = _service(
        universe=["600001", "600002"],
        price_map={
            "600001": [
                _bar(20, open_price=10.0, close=10.1, high=10.2, low=9.9, volume=100),
                _bar(21, open_price=10.1, close=10.2, high=10.3, low=10.0, volume=105),
                _bar(22, open_price=10.2, close=10.3, high=10.4, low=10.1, volume=110),
                _bar(23, open_price=10.3, close=10.4, high=10.5, low=10.2, volume=112),
                _bar(24, open_price=10.4, close=10.5, high=10.6, low=10.3, volume=118),
                _bar(25, open_price=10.5, close=10.9, high=11.0, low=10.4, volume=180),
            ],
            "600002": [
                _bar(20, open_price=10.0, close=9.9, high=10.1, low=9.8, volume=100),
                _bar(21, open_price=9.9, close=9.8, high=10.0, low=9.7, volume=98),
                _bar(22, open_price=9.8, close=9.7, high=9.9, low=9.6, volume=97),
                _bar(23, open_price=9.7, close=9.6, high=9.8, low=9.5, volume=96),
                _bar(24, open_price=9.6, close=9.5, high=9.7, low=9.4, volume=95),
                _bar(25, open_price=9.5, close=9.4, high=9.6, low=9.3, volume=94),
            ],
        },
        articles_by_symbol={},
    )

    results = service.screen_stocks("B1", ["600001", "600002"])

    assert results == [
        ScreeningResult(
            symbol="600001",
            strategy_name="B1",
            passed=True,
            screen_pass_reasons=[
                "close broke above the recent range high",
                "volume expanded versus the recent average",
            ],
            risk_notes=[],
        ),
        ScreeningResult(
            symbol="600002",
            strategy_name="B1",
            passed=False,
            screen_pass_reasons=[],
            risk_notes=["price did not confirm a breakout setup"],
        ),
    ]


def test_filter_negative_news_marks_and_excludes_negative_stocks() -> None:
    service = _service(
        universe=["600001", "600002"],
        price_map={"600001": [], "600002": []},
        articles_by_symbol={
            "600001": [
                NewsArticle(
                    title="600001收到证监会立案告知书",
                    published_at="2026-05-25",
                    summary="公司因涉嫌信息披露违法违规，被中国证监会立案调查。",
                )
            ],
            "600002": [
                NewsArticle(
                    title="600002签订新项目订单",
                    published_at="2026-05-25",
                    sentiment="positive",
                    summary="新订单有望提升收入预期。",
                )
            ],
        },
    )

    results = service.filter_negative_news(["600001", "600002"], lookback_window=3)

    assert results == [
        NewsFilteredStock(
            symbol="600001",
            allowed=False,
            negative_news_flags=["CSRC investigation"],
            risk_notes=["recent material negative news within 3 days"],
        ),
        NewsFilteredStock(
            symbol="600002",
            allowed=True,
            negative_news_flags=[],
            risk_notes=[],
        ),
    ]


def test_filter_negative_news_ignores_false_positive_institutional_research() -> None:
    service = _service(
        universe=["600001"],
        price_map={"600001": []},
        articles_by_symbol={
            "600001": [
                NewsArticle(
                    title="600001接待机构调研",
                    published_at="2026-05-25",
                    summary="本次机构调研围绕新产品进展和未来产能规划展开。",
                )
            ]
        },
    )

    results = service.filter_negative_news(["600001"], lookback_window=7)

    assert results == [
        NewsFilteredStock(
            symbol="600001",
            allowed=True,
            negative_news_flags=[],
            risk_notes=[],
        )
    ]


def test_filter_negative_news_handles_major_shareholder_reduction_plan() -> None:
    service = _service(
        universe=["600001"],
        price_map={"600001": []},
        articles_by_symbol={
            "600001": [
                NewsArticle(
                    title="600001控股股东披露大额减持计划",
                    published_at="2026-05-25",
                    summary="控股股东拟在未来三个月内减持不超过总股本的5%。",
                )
            ]
        },
    )

    results = service.filter_negative_news(["600001"], lookback_window=7)

    assert results == [
        NewsFilteredStock(
            symbol="600001",
            allowed=False,
            negative_news_flags=["major shareholder reduction plan"],
            risk_notes=["recent material negative news within 3 days"],
        )
    ]


def test_score_market_view_returns_stable_scores_and_reasons() -> None:
    bars = [
        _bar(20, open_price=10.0, close=10.1, high=10.2, low=9.9, volume=100),
        _bar(21, open_price=10.1, close=10.2, high=10.3, low=10.0, volume=105),
        _bar(22, open_price=10.2, close=10.3, high=10.4, low=10.1, volume=110),
        _bar(23, open_price=10.3, close=10.4, high=10.5, low=10.2, volume=112),
        _bar(24, open_price=10.4, close=10.5, high=10.6, low=10.3, volume=118),
        _bar(25, open_price=10.5, close=10.9, high=11.0, low=10.4, volume=180),
    ]
    service = _service(
        universe=["600001"],
        price_map={"600001": bars},
        articles_by_symbol={},
    )

    scores = service.score_market_view(["600001"], indicator_profile="default")

    assert scores == [
        ScoredStock(
            symbol="600001",
            technical_score=100,
            score_reasons=[
                "trend is above the short and medium moving averages",
                "latest close is near the session high",
                "volume confirms the move",
            ],
            risk_notes=[],
        )
    ]


def test_run_daily_stock_selection_builds_report_and_preserves_reasons() -> None:
    bars = [
        _bar(20, open_price=10.0, close=10.1, high=10.2, low=9.9, volume=100),
        _bar(21, open_price=10.1, close=10.2, high=10.3, low=10.0, volume=105),
        _bar(22, open_price=10.2, close=10.3, high=10.4, low=10.1, volume=110),
        _bar(23, open_price=10.3, close=10.4, high=10.5, low=10.2, volume=112),
        _bar(24, open_price=10.4, close=10.5, high=10.6, low=10.3, volume=118),
        _bar(25, open_price=10.5, close=10.9, high=11.0, low=10.4, volume=180),
    ]
    service = _service(
        universe=["600001", "600002"],
        price_map={"600001": bars, "600002": list(reversed(bars))},
        articles_by_symbol={
            "600001": [],
            "600002": [
                NewsArticle(
                    title="600002董事长被证监会立案调查",
                    published_at="2026-05-25",
                    summary="监管立案调查显著提升公司治理风险。",
                )
            ],
        },
    )

    report = service.run_daily_stock_selection("B1", date(2026, 5, 26))

    assert report.trade_date == date(2026, 5, 26)
    assert report.strategy_name == "B1"
    assert report.market == "A"
    assert [stock.symbol for stock in report.selected_stocks] == ["600001"]
    assert "600001" in report.summary
    assert "600002" not in report.summary
    assert report.selected_stocks[0].screen_pass_reasons == [
        "close broke above the recent range high",
        "volume expanded versus the recent average",
    ]
    assert report.selected_stocks[0].score_reasons == [
        "trend is above the short and medium moving averages",
        "latest close is near the session high",
        "volume confirms the move",
    ]


def test_run_daily_stock_selection_degrades_when_news_adapter_fails() -> None:
    bars = [
        _bar(20, open_price=10.0, close=10.1, high=10.2, low=9.9, volume=100),
        _bar(21, open_price=10.1, close=10.2, high=10.3, low=10.0, volume=105),
        _bar(22, open_price=10.2, close=10.3, high=10.4, low=10.1, volume=110),
        _bar(23, open_price=10.3, close=10.4, high=10.5, low=10.2, volume=112),
        _bar(24, open_price=10.4, close=10.5, high=10.6, low=10.3, volume=118),
        _bar(25, open_price=10.5, close=10.9, high=11.0, low=10.4, volume=180),
    ]
    service = _service(
        universe=["600001"],
        price_map={"600001": bars},
        articles_by_symbol={"600001": TimeoutError("news timeout")},
    )

    report = service.run_daily_stock_selection("B1", date(2026, 5, 26))

    assert [stock.symbol for stock in report.selected_stocks] == ["600001"]
    assert report.partial_failures == ["news data unavailable for 600001: news timeout"]
    assert "partial data" in report.summary
    assert "news timeout" in report.selected_stocks[0].risk_notes[-1]


def test_run_daily_stock_selection_anchors_news_window_on_trade_date() -> None:
    bars = [
        _bar(20, open_price=10.0, close=10.1, high=10.2, low=9.9, volume=100),
        _bar(21, open_price=10.1, close=10.2, high=10.3, low=10.0, volume=105),
        _bar(22, open_price=10.2, close=10.3, high=10.4, low=10.1, volume=110),
        _bar(23, open_price=10.3, close=10.4, high=10.5, low=10.2, volume=112),
        _bar(24, open_price=10.4, close=10.5, high=10.6, low=10.3, volume=118),
        _bar(25, open_price=10.5, close=10.9, high=11.0, low=10.4, volume=180),
    ]
    news_data = _FakeNewsAdapter(articles_by_symbol={"600001": []})
    service = _service_with_news_adapter(
        universe=["600001"],
        price_map={"600001": bars},
        news_data=news_data,
    )

    service.run_daily_stock_selection("B1", date(2026, 5, 26))

    assert news_data.calls == [("600001", 3, date(2026, 5, 26))]


def test_screen_stocks_b2_strategy_is_deterministic() -> None:
    service = _service(
        universe=["600001", "600002"],
        price_map={
            "600001": [
                _bar(16, open_price=10.0, close=10.0, high=10.1, low=9.9, volume=100),
                _bar(17, open_price=10.0, close=10.1, high=10.2, low=9.9, volume=101),
                _bar(18, open_price=10.1, close=10.2, high=10.3, low=10.0, volume=102),
                _bar(19, open_price=10.2, close=10.3, high=10.4, low=10.1, volume=103),
                _bar(20, open_price=10.3, close=10.4, high=10.5, low=10.2, volume=104),
                _bar(21, open_price=10.4, close=10.5, high=10.6, low=10.3, volume=105),
                _bar(22, open_price=10.5, close=10.6, high=10.7, low=10.4, volume=106),
                _bar(23, open_price=10.6, close=10.7, high=10.8, low=10.5, volume=107),
                _bar(24, open_price=10.7, close=10.8, high=10.9, low=10.6, volume=108),
                _bar(25, open_price=10.8, close=11.0, high=11.1, low=10.7, volume=109),
            ],
            "600002": [
                _bar(16, open_price=10.0, close=10.5, high=10.6, low=9.9, volume=100),
                _bar(17, open_price=10.5, close=10.4, high=10.6, low=10.3, volume=101),
                _bar(18, open_price=10.4, close=10.3, high=10.5, low=10.2, volume=102),
                _bar(19, open_price=10.3, close=10.2, high=10.4, low=10.1, volume=103),
                _bar(20, open_price=10.2, close=10.1, high=10.3, low=10.0, volume=104),
                _bar(21, open_price=10.1, close=10.0, high=10.2, low=9.9, volume=105),
                _bar(22, open_price=10.0, close=9.9, high=10.1, low=9.8, volume=106),
                _bar(23, open_price=9.9, close=9.8, high=10.0, low=9.7, volume=107),
                _bar(24, open_price=9.8, close=9.7, high=9.9, low=9.6, volume=108),
                _bar(25, open_price=9.7, close=9.6, high=9.8, low=9.5, volume=109),
            ],
        },
        articles_by_symbol={},
    )

    results = service.screen_stocks("B2", ["600001", "600002"])

    assert results == [
        ScreeningResult(
            symbol="600001",
            strategy_name="B2",
            passed=True,
            screen_pass_reasons=[
                "short-term moving averages are aligned above medium-term support",
            ],
            risk_notes=[],
        ),
        ScreeningResult(
            symbol="600002",
            strategy_name="B2",
            passed=False,
            screen_pass_reasons=[],
            risk_notes=["moving averages are not in a bullish alignment"],
        ),
    ]


def test_run_daily_stock_selection_rejects_unknown_strategy() -> None:
    service = _service(universe=[], price_map={}, articles_by_symbol={})

    with pytest.raises(DailySelectionServiceError, match="unknown strategy"):
        service.run_daily_stock_selection("missing", date(2026, 5, 26))


@pytest.mark.parametrize(
    "strategy_name",
    ["breakout_volume", "moving_average_alignment", "strong_pullback"],
)
def test_run_daily_stock_selection_rejects_removed_legacy_strategies(strategy_name: str) -> None:
    service = _service(universe=[], price_map={}, articles_by_symbol={})

    with pytest.raises(DailySelectionServiceError, match="unknown strategy"):
        service.run_daily_stock_selection(strategy_name, date(2026, 5, 26))
