"""Subagent-driven stock selection orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from nanobot.stocks.news_adapter import adapt_news_articles
from nanobot.stocks.news_rules import CandidateArticle, prescreen_negative_news
from nanobot.stocks.service import (
    DailySelectionReport,
    DailySelectionServiceError,
    NewsFilteredStock,
    NewsDataAdapter,
    ScoredStock,
    ScreeningResult,
    SelectedStockReport,
)
from nanobot.utils.prompt_templates import render_template

try:
    from langfuse.decorators import langfuse_context
except Exception:  # pragma: no cover - optional dependency
    langfuse_context = None


_SUPPORTED_STRATEGIES = {
    "B1",
    "B2",
}


class InlineSubagentExecutor(Protocol):
    async def run_inline(
        self,
        *,
        task: str,
        label: str,
        temperature: float | None = None,
        extra_system_prompt: str | None = None,
        allow_builtin_tools: bool = True,
        allow_mcp_tools: bool = True,
        use_lightweight_system_prompt: bool = False,
    ) -> str: ...


class TechnicalDataAdapter(Protocol):
    def get_technical_snapshot(
        self,
        symbols: list[str],
        lookback_days: int,
        anchor_date: date | None = None,
    ) -> dict[str, dict[str, Any]]: ...


@dataclass
class StockSelectionSubagentOrchestrator:
    """Run the stock report workflow through isolated subagent stages."""

    executor: InlineSubagentExecutor
    news_data: NewsDataAdapter | None = None
    technical_data: TechnicalDataAdapter | None = None
    market: str = "A"
    workspace: Path = Path(".")
    screening_only: bool = False
    news_filter_only: bool = True
    lookback_days: int = 3

    async def run_daily_stock_selection(self, strategy_name: str, trade_date: date) -> DailySelectionReport:
        if strategy_name not in _SUPPORTED_STRATEGIES:
            raise DailySelectionServiceError(f"unknown strategy: {strategy_name}")

        session_id = self._build_langfuse_session_id(strategy_name, trade_date, self.market)
        input_payload = {
            "strategy_name": strategy_name,
            "trade_date": trade_date.isoformat(),
            "market": self.market,
        }
        self._update_langfuse_root_trace(session_id=session_id, input_payload=input_payload)

        with self._langfuse_span("run_daily_stock_selection"):
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
            # Temporarily disable the news-filter stage and pass stock-screening
            # results directly into market-scoring.
            # review_items, auto_allowed_items, prescreen_failures = self._prepare_news_filter_inputs(
            #     [str(item["symbol"]) for item in screened["items"]],
            #     trade_date=trade_date,
            # )
            # reviewed_items: list[dict[str, Any]] = []
            # reviewed_failures: list[str] = []
            # if review_items:
            #     reviewed_items, reviewed_failures = await self._review_news_candidates(review_items)
            # news_items = [*auto_allowed_items, *reviewed_items]
            # partial_failures = [
            #     *[str(item) for item in prescreen_failures],
            #     *[str(item) for item in reviewed_failures],
            # ]
            # if self.news_filter_only:
            #     return self._build_news_filter_only_report(
            #         strategy_name=strategy_name,
            #         trade_date=trade_date,
            #         screened=screened["items"],
            #         news_items=news_items,
            #         partial_failures=partial_failures,
            #     )
            with self._langfuse_span("prepare-market-scoring-inputs"):
                scoring_items, _ = await self._prepare_market_scoring_inputs(
                    [str(item["symbol"]) for item in screened["items"] if item.get("symbol")],
                    trade_date=trade_date,
                )
            with self._langfuse_span("market-scoring"):
                scored_items, _ = await self._score_market_items_individually(scoring_items)

            with self._langfuse_span("merge-stage-outputs"):
                selected_stocks = self._merge_stage_outputs(
                    strategy_name=strategy_name,
                    trade_date=trade_date,
                    screened=screened["items"],
                    news_items=[],
                    scoring_items=scored_items,
                )

            # Temporarily disable the report-summary stage and return directly
            # after market-scoring completes.
            # summary_payload = await self._run_json_stage(
            #     label="report-summary",
            #     stage="report-summary",
            #     task=self._build_report_summary_task(
            #         [stock.symbol for stock in selected_stocks],
            #     ),
            # )

            return DailySelectionReport(
                trade_date=trade_date,
                strategy_name=strategy_name,
                market=self.market,
                selected_stocks=selected_stocks,
                summary=(
                    f"市场评分已完成，共选出 {len(selected_stocks)} 只股票。"
                ),
                global_risk_disclaimer=(
                    "仅供研究参考，不构成任何投资建议。"
                ),
                partial_failures=[],
            )

    async def _run_json_stage(
        self,
        *,
        label: str,
        stage: str,
        task: str,
        trace_stage: bool = True,
    ) -> dict[str, Any]:
        span_context = self._langfuse_span(stage) if trace_stage else contextlib.nullcontext()
        with span_context:
            raw = await self.executor.run_inline(
                task=task,
                label=label,
                temperature=0.0,
                extra_system_prompt=self._build_stage_system_prompt(stage),
                allow_builtin_tools=stage != "market-scoring",
                allow_mcp_tools=stage != "market-scoring",
                use_lightweight_system_prompt=stage == "market-scoring",
            )
        parsed = self._extract_json(raw, stage)
        if not isinstance(parsed, dict):
            raise DailySelectionServiceError(f"{stage} returned non-object JSON")
        return parsed

    @staticmethod
    def _build_langfuse_session_id(strategy_name: str, trade_date: date, market: str) -> str:
        return f"stock-selection:{market}:{strategy_name}:{trade_date.isoformat()}"

    def _update_langfuse_root_trace(self, *, session_id: str, input_payload: dict[str, Any]) -> None:
        if langfuse_context is None:
            return
        try:
            langfuse_context.update_current_trace(
                name="run_daily_stock_selection",
                session_id=session_id,
                input=input_payload,
            )
        except Exception:
            return

    @staticmethod
    def _langfuse_span(name: str):
        if langfuse_context is None:
            return contextlib.nullcontext()
        try:
            return langfuse_context.start_as_current_span(name=name)
        except Exception:
            return contextlib.nullcontext()

    @staticmethod
    def _extract_json(raw: str, stage: str) -> dict[str, Any]:
        """Robust JSON extraction that tolerates surrounding text and code fences."""
        raw = raw.strip()
        # 1. Try direct parse
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        # 2. Try extracting ```json ... ``` block
        import re
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        # 3. Try first { ... } object (handles trailing text)
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        # 4. Give up
        raise DailySelectionServiceError(
            f"{stage} returned invalid JSON: cannot extract JSON object from output"
        )

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
            summary=f"仅执行股票筛选阶段，共有 {len(selected)} 只候选标的通过。",
            global_risk_disclaimer=(
                "仅供研究参考，本筛选结果不构成任何投资建议。"
            ),
            partial_failures=[],
        )

    def _build_news_filter_only_report(
        self,
        *,
        strategy_name: str,
        trade_date: date,
        screened: list[dict[str, Any]],
        news_items: list[dict[str, Any]],
        partial_failures: list[str],
    ) -> DailySelectionReport:
        news_by_symbol = {str(item["symbol"]): item for item in news_items}
        selected: list[SelectedStockReport] = []

        for item in screened:
            symbol = str(item["symbol"])
            news_raw = news_by_symbol.get(symbol)
            if news_raw is None or not bool(news_raw["allowed"]):
                continue
            selected.append(
                SelectedStockReport(
                    symbol=symbol,
                    strategy_name=str(item.get("strategy_name") or strategy_name),
                    screen_pass_reasons=[str(reason) for reason in item.get("screen_pass_reasons", [])],
                    negative_news_flags=[str(flag) for flag in news_raw.get("negative_news_flags", [])],
                    technical_score=0,
                    score_reasons=[],
                    risk_notes=[
                        *[str(note) for note in item.get("risk_notes", [])],
                        *[str(note) for note in news_raw.get("risk_notes", [])],
                    ],
                    report_date=trade_date,
                )
            )

        selected.sort(key=lambda item: item.symbol)
        return DailySelectionReport(
            trade_date=trade_date,
            strategy_name=strategy_name,
            market=self.market,
            selected_stocks=selected,
            summary=(
                f"仅执行筛选与新闻过滤阶段，共有 {len(selected)} 只候选标的通过。"
            ),
            global_risk_disclaimer=(
                "仅供研究参考，本结果不构成任何投资建议。"
            ),
            partial_failures=[],
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
        )

    def _build_news_filter_task(self, items: list[dict[str, Any]]) -> str:
        return render_template(
            "stocks/tasks/news_filter.md",
            strip=True,
            items_json=json.dumps(items, ensure_ascii=False),
            lookback_days=self.lookback_days,
        )

    def _build_market_scoring_task(self, items: list[dict[str, Any]]) -> str:
        return render_template(
            "stocks/tasks/market_scoring.md",
            strip=True,
            items_json=json.dumps(items, ensure_ascii=False),
        )

    async def _score_market_items_individually(
        self,
        scoring_items: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        scored_items: list[dict[str, Any]] = []
        partial_failures: list[str] = []

        for item in scoring_items:
            symbol = str(item.get("symbol", ""))
            try:
                with self._langfuse_span(f"market-scoring:{symbol or 'unknown'}"):
                    scoring = await self._run_json_stage(
                        label="market-scoring",
                        stage="market-scoring",
                        task=self._build_market_scoring_task([item]),
                        trace_stage=False,
                    )
                raw_items = scoring.get("items")
                if not isinstance(raw_items, list) or len(raw_items) != 1:
                    raise ValueError("expected exactly one scored item")
                scored_item = raw_items[0]
                if not isinstance(scored_item, dict):
                    raise TypeError("scored item must be a JSON object")
                scored_symbol = str(scored_item.get("symbol", ""))
                if scored_symbol != symbol:
                    raise ValueError(f"expected symbol {symbol}, got {scored_symbol or 'missing'}")
                scored_items.append(scored_item)
            except Exception as exc:
                partial_failures.append(f"market scoring unavailable for {symbol}: {exc}")

        return scored_items, partial_failures

    async def _review_news_candidates(
        self,
        review_items: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        reviewed_items: list[dict[str, Any]] = []
        partial_failures: list[str] = []

        for item in review_items:
            reviewed_news = await self._run_json_stage(
                label="news-filter",
                stage="news-filter",
                task=self._build_news_filter_task([item]),
            )
            reviewed_items.extend(reviewed_news.get("items", []))
            partial_failures.extend([str(entry) for entry in reviewed_news.get("partial_failures", [])])

        return reviewed_items, partial_failures

    def _prepare_news_filter_inputs(
        self,
        symbols: list[str],
        trade_date: date | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        review_items: list[dict[str, Any]] = []
        auto_allowed_items: list[dict[str, Any]] = []
        partial_failures: list[str] = []

        if self.news_data is None:
            for symbol in symbols:
                review_items.append(
                    {
                        "symbol": symbol,
                        "has_negative_candidates": False,
                        "candidate_articles": [],
                    }
                )
            return review_items, auto_allowed_items, partial_failures

        for symbol in symbols:
            try:
                raw_articles = self.news_data.get_news(
                    symbol,
                    self.lookback_days,
                    anchor_date=trade_date,
                )
            except Exception as exc:
                message = f"news data unavailable for {symbol}: {exc}"
                partial_failures.append(message)
                auto_allowed_items.append(
                    {
                        "symbol": symbol,
                        "allowed": True,
                        "decision": "REVIEW",
                        "matched_categories": [],
                        "negative_news_flags": [],
                        "risk_notes": [message],
                        "evidence": [],
                    }
                )
                continue

            prescreened = prescreen_negative_news(symbol, adapt_news_articles(raw_articles))
            if not prescreened.has_negative_candidates:
                auto_allowed_items.append(
                    {
                        "symbol": symbol,
                        "allowed": True,
                        "decision": "PASS",
                        "matched_categories": [],
                        "negative_news_flags": [],
                        "risk_notes": [],
                        "evidence": [],
                    }
                )
                continue

            review_items.append(
                {
                    "symbol": symbol,
                    "has_negative_candidates": True,
                    "candidate_articles": [
                        self._candidate_article_to_dict(article)
                        for article in prescreened.candidate_articles
                    ],
                }
            )

        return review_items, auto_allowed_items, partial_failures

    async def _prepare_market_scoring_inputs(
        self,
        symbols: list[str],
        trade_date: date | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        scoring_items: list[dict[str, Any]] = []
        partial_failures: list[str] = []

        if self.technical_data is None:
            technical_snapshots: dict[str, dict[str, Any]] | None = None
        else:
            try:
                batch_result = await asyncio.to_thread(
                    self.technical_data.get_technical_snapshot,
                    symbols,
                    60,
                    trade_date,
                )
                if not isinstance(batch_result, dict):
                    raise TypeError("batch technical snapshot response must be a JSON object")
                technical_snapshots = {
                    str(key): value
                    for key, value in batch_result.items()
                    if isinstance(key, str) and isinstance(value, dict)
                }
            except Exception as exc:
                technical_snapshots = {}
                message = f"technical data unavailable for batch {symbols}: {exc}"
                partial_failures.append(message)

        for symbol in symbols:
            technical_snapshot: dict[str, Any]
            if self.technical_data is None:
                technical_snapshot = self._build_unavailable_technical_snapshot(
                    symbol,
                    "technical data adapter not configured",
                )
            else:
                technical_snapshot = technical_snapshots.get(symbol) if technical_snapshots is not None else None
                if technical_snapshot is None:
                    if partial_failures and partial_failures[-1].startswith("technical data unavailable for batch "):
                        message = f"technical data unavailable for {symbol}: batch request failed"
                    else:
                        message = (
                            f"technical data unavailable for {symbol}: "
                            "snapshot missing from batch response"
                        )
                    if message not in partial_failures:
                        partial_failures.append(message)
                    technical_snapshot = self._build_unavailable_technical_snapshot(symbol, message)

            scoring_snapshot = self._flatten_scoring_snapshot(technical_snapshot)
            scoring_items.append(
                {
                    "symbol": symbol,
                    "technical_snapshot": scoring_snapshot,
                }
            )

        return scoring_items, partial_failures

    @staticmethod
    def _flatten_scoring_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        """Reduce adapter payloads to the scoring fields the LLM actually needs."""
        inner = snapshot.get("technical_snapshot")
        if isinstance(inner, dict):
            flattened = dict(inner)
            # Preserve error/status metadata that may live on the wrapper object.
            if "data_status" in snapshot and "data_status" not in flattened:
                flattened["data_status"] = snapshot["data_status"]
            if "reason" in snapshot and "reason" not in flattened:
                flattened["reason"] = snapshot["reason"]
            if "symbol" in snapshot and "symbol" not in flattened:
                flattened["symbol"] = snapshot["symbol"]
            return flattened
        return snapshot

    @staticmethod
    def _build_unavailable_technical_snapshot(symbol: str, reason: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "data_status": "unavailable",
            "reason": reason,
        }

    @staticmethod
    def _candidate_article_to_dict(article: CandidateArticle) -> dict[str, Any]:
        return {
            "date": article.date,
            "title": article.title,
            "matched_keywords": list(article.matched_keywords),
            "candidate_categories": list(article.candidate_categories),
            "rule_severity": article.rule_severity,
        }

    def _build_report_summary_task(self, selected_symbols: list[str]) -> str:
        return render_template(
            "stocks/tasks/report_summary.md",
            strip=True,
            selected_json=json.dumps(selected_symbols, ensure_ascii=False),
        )

    @staticmethod
    def _merge_stage_outputs(
        *,
        strategy_name: str,
        trade_date: date,
        screened: list[dict[str, Any]],
        news_items: list[dict[str, Any]],
        scoring_items: list[dict[str, Any]],
    ) -> list[SelectedStockReport]:
        del screened, news_items
        selected: list[SelectedStockReport] = []

        for scoring_raw in scoring_items:
            scoring = ScoredStock(
                symbol=str(scoring_raw["symbol"]),
                technical_score=int(scoring_raw["technical_score"]),
                score_reasons=[str(reason) for reason in scoring_raw.get("score_reasons", [])],
                risk_notes=[str(note) for note in scoring_raw.get("risk_notes", [])],
            )
            selected.append(
                SelectedStockReport(
                    symbol=scoring.symbol,
                    strategy_name=strategy_name,
                    screen_pass_reasons=[],
                    negative_news_flags=[],
                    technical_score=scoring.technical_score,
                    score_reasons=scoring.score_reasons,
                    risk_notes=scoring.risk_notes,
                    report_date=trade_date,
                )
            )

        selected.sort(key=lambda item: (-item.technical_score, item.symbol))
        return selected[:10]
