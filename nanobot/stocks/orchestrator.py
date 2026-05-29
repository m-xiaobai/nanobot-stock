"""Subagent-driven stock selection orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from nanobot.stocks.service import (
    DailySelectionReport,
    DailySelectionServiceError,
    NewsFilteredStock,
    ScoredStock,
    ScreeningResult,
    SelectedStockReport,
)
from nanobot.utils.prompt_templates import render_template


_SUPPORTED_STRATEGIES = {
    "breakout_volume",
    "moving_average_alignment",
    "strong_pullback",
}

_STRATEGY_SKILL_DIRS = {
    "breakout_volume": "breakout-volume",
    "moving_average_alignment": "moving-average-alignment",
    "strong_pullback": "strong-pullback",
}


class InlineSubagentExecutor(Protocol):
    async def run_inline(
        self,
        *,
        task: str,
        label: str,
        temperature: float | None = None,
        extra_system_prompt: str | None = None,
    ) -> str: ...


@dataclass
class StockSelectionSubagentOrchestrator:
    """Run the stock report workflow through isolated subagent stages."""

    executor: InlineSubagentExecutor
    market: str = "A"
    workspace: Path = Path(".")
    screening_only: bool = True

    async def run_daily_stock_selection(self, strategy_name: str, trade_date: date) -> DailySelectionReport:
        if strategy_name not in _SUPPORTED_STRATEGIES:
            raise DailySelectionServiceError(f"unknown strategy: {strategy_name}")

        screened = await self._run_json_stage(
            label="stock-screening",
            stage="stock-screening",
            task=self._build_stock_screening_task(strategy_name, trade_date),
        )
        if self.screening_only:
            return self._build_screening_only_report(
                strategy_name=strategy_name,
                trade_date=trade_date,
                screened=screened["items"],
            )
        news = await self._run_json_stage(
            label="news-filter",
            stage="news-filter",
            task=self._build_news_filter_task(
                [item["symbol"] for item in screened["items"]]
            ),
        )
        scoring = await self._run_json_stage(
            label="market-scoring",
            stage="market-scoring",
            task=self._build_market_scoring_task(
                [item["symbol"] for item in news["items"] if item["allowed"]]
            ),
        )

        selected_stocks = self._merge_stage_outputs(
            strategy_name=strategy_name,
            trade_date=trade_date,
            screened=screened["items"],
            news_items=news["items"],
            scoring_items=scoring["items"],
        )

        summary_payload = await self._run_json_stage(
            label="report-summary",
            stage="report-summary",
            task=self._build_report_summary_task(
                [stock.symbol for stock in selected_stocks],
            ),
        )

        return DailySelectionReport(
            trade_date=trade_date,
            strategy_name=strategy_name,
            market=self.market,
            selected_stocks=selected_stocks,
            summary=str(summary_payload["summary"]),
            global_risk_disclaimer=str(summary_payload["global_risk_disclaimer"]),
            partial_failures=[str(item) for item in news.get("partial_failures", [])],
        )

    async def _run_json_stage(self, *, label: str, stage: str, task: str) -> dict[str, Any]:
        raw = await self.executor.run_inline(
            task=task,
            label=label,
            temperature=0.0,
            extra_system_prompt=self._build_stage_system_prompt(stage),
        )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DailySelectionServiceError(f"{stage} returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise DailySelectionServiceError(f"{stage} returned non-object JSON")
        return parsed

    def _build_screening_only_report(
        self,
        *,
        strategy_name: str,
        trade_date: date,
        screened: list[dict[str, Any]],
    ) -> DailySelectionReport:
        selected = [
            SelectedStockReport(
                symbol=str(item["symbol"]),
                strategy_name=str(item.get("strategy_name") or strategy_name),
                screen_pass_reasons=[str(reason) for reason in item.get("screen_pass_reasons", [])],
                negative_news_flags=[],
                technical_score=0,
                score_reasons=[],
                risk_notes=[str(note) for note in item.get("risk_notes", [])],
                report_date=trade_date,
            )
            for item in screened
        ]
        selected.sort(key=lambda item: item.symbol)
        return DailySelectionReport(
            trade_date=trade_date,
            strategy_name=strategy_name,
            market=self.market,
            selected_stocks=selected,
            summary=f"Screening-only mode: {len(selected)} candidate(s) passed stock-screening.",
            global_risk_disclaimer=(
                "For research use only. This screening-only report is not investment advice."
            ),
            partial_failures=[
                "screening_only mode enabled; skipped news-filter, market-scoring, report-summary"
            ],
        )

    def _build_stage_system_prompt(self, stage: str) -> str:
        builders = {
            "stock-screening": self._build_stock_screening_system_prompt,
            "news-filter": self._build_news_filter_system_prompt,
            "market-scoring": self._build_market_scoring_system_prompt,
            "report-summary": self._build_report_summary_system_prompt,
        }
        try:
            return builders[stage]()
        except KeyError as exc:
            raise DailySelectionServiceError(f"unknown stage: {stage}") from exc

    def _build_stock_screening_system_prompt(self) -> str:
        return render_template("stocks/system/stock_screening.md", strip=True)

    def _build_news_filter_system_prompt(self) -> str:
        return render_template("stocks/system/news_filter.md", strip=True)

    def _build_market_scoring_system_prompt(self) -> str:
        return render_template("stocks/system/market_scoring.md", strip=True)

    def _build_report_summary_system_prompt(self) -> str:
        return render_template("stocks/system/report_summary.md", strip=True)

    def _build_stock_screening_task(self, strategy_name: str, trade_date: date) -> str:
        return render_template(
            "stocks/tasks/stock_screening.md",
            strip=True,
            trade_date=trade_date.isoformat(),
            strategy_name=strategy_name,
            strategy_skill_path=self._strategy_skill_path(strategy_name).as_posix(),
        )

    def _build_news_filter_task(self, symbols: list[str]) -> str:
        return render_template(
            "stocks/tasks/news_filter.md",
            strip=True,
            symbols_json=json.dumps(symbols, ensure_ascii=False),
        )

    def _build_market_scoring_task(self, symbols: list[str]) -> str:
        return render_template(
            "stocks/tasks/market_scoring.md",
            strip=True,
            symbols_json=json.dumps(symbols, ensure_ascii=False),
        )

    def _build_report_summary_task(self, selected_symbols: list[str]) -> str:
        return render_template(
            "stocks/tasks/report_summary.md",
            strip=True,
            selected_json=json.dumps(selected_symbols, ensure_ascii=False),
        )

    def _strategy_skill_path(self, strategy_name: str) -> Path:
        try:
            skill_dir = _STRATEGY_SKILL_DIRS[strategy_name]
        except KeyError as exc:
            raise DailySelectionServiceError(f"unknown strategy: {strategy_name}") from exc
        return self.workspace / "skills" / skill_dir / "SKILL.md"

    @staticmethod
    def _merge_stage_outputs(
        *,
        strategy_name: str,
        trade_date: date,
        screened: list[dict[str, Any]],
        news_items: list[dict[str, Any]],
        scoring_items: list[dict[str, Any]],
    ) -> list[SelectedStockReport]:
        news_by_symbol = {str(item["symbol"]): item for item in news_items}
        scoring_by_symbol = {str(item["symbol"]): item for item in scoring_items}
        selected: list[SelectedStockReport] = []

        for item in screened:
            screen = ScreeningResult(
                symbol=str(item["symbol"]),
                strategy_name=str(item.get("strategy_name") or strategy_name),
                screen_pass_reasons=[str(reason) for reason in item.get("screen_pass_reasons", [])],
                risk_notes=[str(note) for note in item.get("risk_notes", [])],
            )

            news_raw = news_by_symbol.get(screen.symbol)
            if news_raw is None:
                continue
            news = NewsFilteredStock(
                symbol=str(news_raw["symbol"]),
                allowed=bool(news_raw["allowed"]),
                negative_news_flags=[str(flag) for flag in news_raw.get("negative_news_flags", [])],
                risk_notes=[str(note) for note in news_raw.get("risk_notes", [])],
            )
            if not news.allowed:
                continue

            scoring_raw = scoring_by_symbol.get(screen.symbol)
            if scoring_raw is None:
                continue
            scoring = ScoredStock(
                symbol=str(scoring_raw["symbol"]),
                technical_score=int(scoring_raw["technical_score"]),
                score_reasons=[str(reason) for reason in scoring_raw.get("score_reasons", [])],
                risk_notes=[str(note) for note in scoring_raw.get("risk_notes", [])],
            )
            selected.append(
                SelectedStockReport(
                    symbol=screen.symbol,
                    strategy_name=screen.strategy_name,
                    screen_pass_reasons=screen.screen_pass_reasons,
                    negative_news_flags=news.negative_news_flags,
                    technical_score=scoring.technical_score,
                    score_reasons=scoring.score_reasons,
                    risk_notes=[*screen.risk_notes, *news.risk_notes, *scoring.risk_notes],
                    report_date=trade_date,
                )
            )

        selected.sort(key=lambda item: (-item.technical_score, item.symbol))
        return selected
