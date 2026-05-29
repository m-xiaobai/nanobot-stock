"""Rule-driven daily stock selection workflow for the MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from statistics import mean
from typing import Protocol


class DailySelectionServiceError(ValueError):
    """Raised when the stock selection request is invalid."""


@dataclass(frozen=True)
class StockUniverseRequest:
    market: str


@dataclass(frozen=True)
class StockUniverseResponse:
    market: str
    symbols: list[str]


@dataclass(frozen=True)
class PriceSeriesRequest:
    symbol: str
    lookback_days: int


@dataclass(frozen=True)
class PriceBar:
    trade_date: date
    open: float
    close: float
    high: float
    low: float
    volume: float


@dataclass(frozen=True)
class PriceSeriesResponse:
    symbol: str
    bars: list[PriceBar]


@dataclass(frozen=True)
class NewsQueryRequest:
    symbol: str
    lookback_days: int


@dataclass(frozen=True)
class NewsArticle:
    title: str
    published_at: str
    sentiment: str = "neutral"
    summary: str = ""


@dataclass(frozen=True)
class ScreeningResult:
    symbol: str
    strategy_name: str
    screen_pass_reasons: list[str]
    risk_notes: list[str]
    passed: bool = True


@dataclass(frozen=True)
class NewsFilteredStock:
    symbol: str
    allowed: bool
    negative_news_flags: list[str]
    risk_notes: list[str]


@dataclass(frozen=True)
class ScoredStock:
    symbol: str
    technical_score: int
    score_reasons: list[str]
    risk_notes: list[str]


@dataclass(frozen=True)
class SelectedStockReport:
    symbol: str
    strategy_name: str
    screen_pass_reasons: list[str]
    negative_news_flags: list[str]
    technical_score: int
    score_reasons: list[str]
    risk_notes: list[str]
    report_date: date


@dataclass(frozen=True)
class DailySelectionReport:
    trade_date: date
    strategy_name: str
    market: str
    selected_stocks: list[SelectedStockReport]
    summary: str
    global_risk_disclaimer: str
    partial_failures: list[str] = field(default_factory=list)


class MarketDataAdapter(Protocol):
    def get_stock_universe(self, market: str) -> StockUniverseResponse: ...

    def get_price_series(self, symbol: str, lookback_days: int) -> PriceSeriesResponse: ...


class NewsDataAdapter(Protocol):
    def get_news(self, symbol: str, lookback_days: int) -> list[NewsArticle]: ...


@dataclass(frozen=True)
class StrategyDefinition:
    name: str
    lookback_days: int


_SUPPORTED_STRATEGIES = {
    "breakout_volume": StrategyDefinition(name="breakout_volume", lookback_days=30),
    "moving_average_alignment": StrategyDefinition(name="moving_average_alignment", lookback_days=30),
    "strong_pullback": StrategyDefinition(name="strong_pullback", lookback_days=30),
}


class DailySelectionService:
    """Coordinates rule-driven screening, filtering, scoring, and reporting."""

    def __init__(
        self,
        *,
        market_data: MarketDataAdapter,
        news_data: NewsDataAdapter,
        market: str = "A",
    ) -> None:
        self._market_data = market_data
        self._news_data = news_data
        self._market = market

    def run_daily_stock_selection(self, strategy_name: str, trade_date: date) -> DailySelectionReport:
        strategy = self._strategy(strategy_name)
        universe = self._market_data.get_stock_universe(self._market).symbols
        screened = self.screen_stocks(strategy.name, universe)

        selected_stocks: list[SelectedStockReport] = []
        partial_failures: list[str] = []

        for result in screened:
            if not result.passed:
                continue

            filter_result, filter_failure = self._safe_news_filter(result.symbol)
            if filter_failure is not None:
                partial_failures.append(filter_failure)

            if not filter_result.allowed:
                continue

            scored = self.score_market_view([result.symbol], indicator_profile="default")[0]
            selected_stocks.append(
                SelectedStockReport(
                    symbol=result.symbol,
                    strategy_name=strategy.name,
                    screen_pass_reasons=result.screen_pass_reasons,
                    negative_news_flags=filter_result.negative_news_flags,
                    technical_score=scored.technical_score,
                    score_reasons=scored.score_reasons,
                    risk_notes=[*scored.risk_notes, *filter_result.risk_notes],
                    report_date=trade_date,
                )
            )

        selected_stocks.sort(key=lambda item: (-item.technical_score, item.symbol))
        summary = self._build_summary(selected_stocks, partial_failures)
        return DailySelectionReport(
            trade_date=trade_date,
            strategy_name=strategy.name,
            market=self._market,
            selected_stocks=selected_stocks,
            summary=summary,
            global_risk_disclaimer=(
                "For research use only. This report is not investment advice and does not "
                "constitute an instruction to trade."
            ),
            partial_failures=partial_failures,
        )

    def screen_stocks(self, strategy_name: str, universe: list[str]) -> list[ScreeningResult]:
        strategy = self._strategy(strategy_name)
        results: list[ScreeningResult] = []
        for symbol in universe:
            bars = self._get_bars(symbol, strategy.lookback_days)
            if strategy.name == "breakout_volume":
                results.append(self._screen_breakout_volume(symbol, bars))
            elif strategy.name == "moving_average_alignment":
                results.append(self._screen_moving_average_alignment(symbol, bars))
            else:
                results.append(self._screen_strong_pullback(symbol, bars))
        return results

    def filter_negative_news(self, symbols: list[str], lookback_window: int) -> list[NewsFilteredStock]:
        return [self._filter_symbol_news(symbol, lookback_window) for symbol in symbols]

    def score_market_view(self, symbols: list[str], indicator_profile: str) -> list[ScoredStock]:
        del indicator_profile
        scored: list[ScoredStock] = []
        for symbol in symbols:
            bars = self._get_bars(symbol, 30)
            scored.append(self._score_symbol(symbol, bars))
        return scored

    def _strategy(self, strategy_name: str) -> StrategyDefinition:
        strategy = _SUPPORTED_STRATEGIES.get(strategy_name)
        if strategy is None:
            raise DailySelectionServiceError(f"unknown strategy: {strategy_name}")
        return strategy

    def _get_bars(self, symbol: str, lookback_days: int) -> list[PriceBar]:
        return self._market_data.get_price_series(symbol, lookback_days).bars

    def _screen_breakout_volume(self, symbol: str, bars: list[PriceBar]) -> ScreeningResult:
        if len(bars) < 6:
            return ScreeningResult(
                symbol=symbol,
                strategy_name="breakout_volume",
                passed=False,
                screen_pass_reasons=[],
                risk_notes=["not enough price history for breakout screening"],
            )
        latest = bars[-1]
        prior = bars[-6:-1]
        prior_high = max(bar.high for bar in prior)
        avg_volume = mean(bar.volume for bar in prior)
        reasons: list[str] = []
        if latest.close > prior_high:
            reasons.append("close broke above the recent range high")
        if latest.volume > avg_volume * 1.4:
            reasons.append("volume expanded versus the recent average")
        if len(reasons) == 2:
            return ScreeningResult(
                symbol=symbol,
                strategy_name="breakout_volume",
                passed=True,
                screen_pass_reasons=reasons,
                risk_notes=[],
            )
        return ScreeningResult(
            symbol=symbol,
            strategy_name="breakout_volume",
            passed=False,
            screen_pass_reasons=[],
            risk_notes=["price did not confirm a breakout setup"],
        )

    def _screen_moving_average_alignment(self, symbol: str, bars: list[PriceBar]) -> ScreeningResult:
        if len(bars) < 10:
            return ScreeningResult(
                symbol=symbol,
                strategy_name="moving_average_alignment",
                passed=False,
                screen_pass_reasons=[],
                risk_notes=["not enough price history for moving average alignment"],
            )
        closes = [bar.close for bar in bars]
        ma3 = mean(closes[-3:])
        ma5 = mean(closes[-5:])
        ma10 = mean(closes[-10:])
        if closes[-1] > ma3 > ma5 > ma10:
            return ScreeningResult(
                symbol=symbol,
                strategy_name="moving_average_alignment",
                passed=True,
                screen_pass_reasons=[
                    "short-term moving averages are aligned above medium-term support",
                ],
                risk_notes=[],
            )
        return ScreeningResult(
            symbol=symbol,
            strategy_name="moving_average_alignment",
            passed=False,
            screen_pass_reasons=[],
            risk_notes=["moving averages are not in a bullish alignment"],
        )

    def _screen_strong_pullback(self, symbol: str, bars: list[PriceBar]) -> ScreeningResult:
        if len(bars) < 6:
            return ScreeningResult(
                symbol=symbol,
                strategy_name="strong_pullback",
                passed=False,
                screen_pass_reasons=[],
                risk_notes=["not enough price history for pullback screening"],
            )
        recent_peak = max(bar.high for bar in bars[-6:-1])
        latest = bars[-1]
        prior_avg_volume = mean(bar.volume for bar in bars[-6:-1])
        if latest.close < recent_peak and latest.close > recent_peak * 0.95 and latest.volume < prior_avg_volume:
            return ScreeningResult(
                symbol=symbol,
                strategy_name="strong_pullback",
                passed=True,
                screen_pass_reasons=[
                    "price is consolidating above the recent breakout zone",
                    "pullback volume is lower than the prior advance",
                ],
                risk_notes=[],
            )
        return ScreeningResult(
            symbol=symbol,
            strategy_name="strong_pullback",
            passed=False,
            screen_pass_reasons=[],
            risk_notes=["pullback quality is not strong enough"],
        )

    def _safe_news_filter(self, symbol: str) -> tuple[NewsFilteredStock, str | None]:
        try:
            return self._filter_symbol_news(symbol, 3), None
        except Exception as exc:
            message = f"news data unavailable for {symbol}: {exc}"
            return (
                NewsFilteredStock(
                    symbol=symbol,
                    allowed=True,
                    negative_news_flags=[],
                    risk_notes=[message],
                ),
                message,
            )

    def _filter_symbol_news(self, symbol: str, lookback_window: int) -> NewsFilteredStock:
        articles = self._news_data.get_news(symbol, lookback_window)
        negative_titles = [
            article.title
            for article in articles
            if article.sentiment.lower() == "negative"
            or any(
                keyword in article.title.lower()
                for keyword in ("inquiry", "investigation", "default", "lawsuit", "freeze", "risk")
            )
        ]
        if negative_titles:
            return NewsFilteredStock(
                symbol=symbol,
                allowed=False,
                negative_news_flags=negative_titles,
                risk_notes=["recent negative news flow requires exclusion from the shortlist"],
            )
        return NewsFilteredStock(symbol=symbol, allowed=True, negative_news_flags=[], risk_notes=[])

    def _score_symbol(self, symbol: str, bars: list[PriceBar]) -> ScoredStock:
        if len(bars) < 6:
            return ScoredStock(
                symbol=symbol,
                technical_score=0,
                score_reasons=[],
                risk_notes=["not enough price history for technical scoring"],
            )
        latest = bars[-1]
        closes = [bar.close for bar in bars]
        ma3 = mean(closes[-3:])
        ma5 = mean(closes[-5:])
        avg_volume = mean(bar.volume for bar in bars[-6:-1])

        score = 0
        reasons: list[str] = []
        if latest.close >= ma3 >= ma5:
            score += 35
            reasons.append("trend is above the short and medium moving averages")
        if latest.high > latest.low:
            close_position = (latest.close - latest.low) / (latest.high - latest.low)
            if close_position >= 0.8:
                score += 30
                reasons.append("latest close is near the session high")
        if latest.volume >= avg_volume * 1.4:
            score += 30
            reasons.append("volume confirms the move")
        if latest.close > bars[-2].close:
            score += 5
        return ScoredStock(
            symbol=symbol,
            technical_score=min(score, 100),
            score_reasons=reasons,
            risk_notes=[],
        )

    def _build_summary(
        self,
        selected_stocks: list[SelectedStockReport],
        partial_failures: list[str],
    ) -> str:
        chosen = ", ".join(stock.symbol for stock in selected_stocks) or "none"
        summary = f"Selected candidates: {chosen}."
        if partial_failures:
            summary += " Report generated with partial data because some external dependencies were unavailable."
        return summary
