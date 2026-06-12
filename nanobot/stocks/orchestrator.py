"""Subagent-driven stock selection orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from datetime import date, datetime
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
    from langfuse import get_client, propagate_attributes
except Exception:  # pragma: no cover - optional dependency
    get_client = None
    propagate_attributes = None


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
    lookback_days: int = 7
    market_scoring_max_concurrency: int = 5

    async def run_daily_stock_selection(self, strategy_name: str, trade_date: date) -> DailySelectionReport:
        if strategy_name not in _SUPPORTED_STRATEGIES:
            raise DailySelectionServiceError(f"unknown strategy: {strategy_name}")

        session_id = self._build_langfuse_session_id(strategy_name, trade_date, self.market)
        with self._langfuse_span("run_daily_stock_selection"):
            with self._langfuse_attributes(
                session_id=session_id,
                metadata={
                    "strategy_name": strategy_name,
                    "trade_date": trade_date.isoformat(),
                    "market": self.market,
                },
            ):
                # stock screening
                screened = await self._run_json_stage(
                    label="stock-screening",
                    stage="stock-screening",
                    task=self._build_stock_screening_task(strategy_name, trade_date),
                )
                
                # news filtering
                with self._langfuse_span("prepare-news-filter-inputs"):
                    review_items, auto_allowed_items = await self._prepare_news_filter_inputs(
                        screened["items"],
                        trade_date=trade_date,
                    )
                    self._langfuse_observation_payload(
                        input_payload={
                            "trade_date": trade_date.isoformat(),
                            "screened_count": len(screened["items"]),
                            "screened_items": screened["items"],
                        },
                        output_payload={
                            "review_items_count": len(review_items),
                            "review_items": review_items,
                            "auto_allowed_items_count": len(auto_allowed_items),
                            "auto_allowed_items": auto_allowed_items,
                        },
                    )
                reviewed_items: list[dict[str, Any]] = []
                news_filter_failures: list[str] = []
                if review_items:
                    with self._langfuse_span("news-filter"):
                        reviewed_items, news_filter_failures = await self._review_news_candidates(review_items)
                        self._langfuse_observation_payload(
                            input_payload={
                                "trade_date": trade_date.isoformat(),
                                "review_items_count": len(review_items),
                                "review_items": review_items,
                            },
                            output_payload={
                                "reviewed_items_count": len(reviewed_items),
                                "reviewed_items": reviewed_items,
                                "news_filter_failures": news_filter_failures,
                            },
                        )
                news_items = [*auto_allowed_items, *reviewed_items]
                
                #market scoring
                with self._langfuse_span("prepare-market-scoring-inputs"):
                    scoring_items = await self._prepare_market_scoring_inputs(
                        [
                            str(item["symbol"])
                            for item in news_items
                            if item.get("allowed") and item.get("symbol")
                        ],
                        trade_date=trade_date,
                    )
                
                with self._langfuse_span("market-scoring"):
                    scored_items, market_scoring_failures = await self._score_market_items_individually(
                        scoring_items
                    )

                with self._langfuse_span("merge-stage-outputs"):
                    selected_stocks = self._merge_stage_outputs(
                        strategy_name=strategy_name,
                        trade_date=trade_date,
                        screened=screened["items"],
                        news_items=news_items,
                        scoring_items=scored_items,
                    )
                    self._langfuse_observation_payload(
                        input_payload={
                            "strategy_name": strategy_name,
                            "trade_date": trade_date.isoformat(),
                            "screened_count": len(screened["items"]),
                            "news_items_count": len(news_items),
                            "scoring_items": scored_items,
                        },
                        output_payload={
                            "selected_count": len(selected_stocks),
                            "selected_stocks": [
                                {
                                    "symbol": item.symbol,
                                    "technical_score": item.technical_score,
                                    "score_reasons": item.score_reasons,
                                    "risk_notes": item.risk_notes,
                                }
                                for item in selected_stocks
                            ],
                        },
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
                    partial_failures=[*news_filter_failures, *market_scoring_failures],
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
            with self._langfuse_metadata({"stage": stage}):
                raw = await self.executor.run_inline(
                    task=task,
                    label=label,
                    temperature=0.0,
                    extra_system_prompt=self._build_stage_system_prompt(stage),
                    allow_builtin_tools=stage not in {"news-filter", "market-scoring"},
                    allow_mcp_tools=stage not in {"news-filter", "market-scoring"},
                    use_lightweight_system_prompt=stage in {"news-filter", "market-scoring"},
                )
        parsed = self._extract_json(raw, stage)
        if not isinstance(parsed, dict):
            raise DailySelectionServiceError(f"{stage} returned non-object JSON")
        return parsed

    @staticmethod
    def _build_langfuse_session_id(strategy_name: str, trade_date: date, market: str) -> str:
        run_ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        return (
            f"stock-selection:{market}:{strategy_name}:"
            f"{trade_date.isoformat()}:run-{run_ts}"
        )

    @staticmethod
    def _langfuse_span(name: str):
        if get_client is None:
            return contextlib.nullcontext()
        try:
            return get_client().start_as_current_observation(name=name, as_type="span")
        except Exception:
            return contextlib.nullcontext()

    @staticmethod
    def _langfuse_attributes(*, session_id: str, metadata: dict[str, Any]):
        if propagate_attributes is None:
            return contextlib.nullcontext()
        try:
            return propagate_attributes(session_id=session_id, metadata=metadata)
        except Exception:
            return contextlib.nullcontext()

    @staticmethod
    def _langfuse_metadata(metadata: dict[str, Any]):
        if propagate_attributes is None:
            return contextlib.nullcontext()
        try:
            return propagate_attributes(metadata=metadata)
        except Exception:
            return contextlib.nullcontext()

    @staticmethod
    def _langfuse_observation_payload(
        *,
        input_payload: dict[str, Any] | None = None,
        output_payload: dict[str, Any] | None = None,
    ) -> None:
        if get_client is None:
            return
        try:
            client = get_client()
        except Exception:
            return
        if client is None:
            return

        update = getattr(client, "update_current_observation", None)
        if not callable(update):
            update = getattr(client, "update_current_span", None)
        if not callable(update):
            return

        payload: dict[str, Any] = {}
        if input_payload is not None:
            payload["input"] = input_payload
        if output_payload is not None:
            payload["output"] = output_payload
        if not payload:
            return

        try:
            update(**payload)
        except Exception:
            return

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
                screen_pass_reasons=[],
                technical_score=0,
                score_reasons=[],
                risk_notes=[],
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
                    screen_pass_reasons=[],
                    technical_score=0,
                    score_reasons=[],
                    risk_notes=[
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
            partial_failures=partial_failures,
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
        if not scoring_items:
            return [], []

        concurrency = max(1, self.market_scoring_max_concurrency)
        gate = asyncio.Semaphore(concurrency)

        async def _score_one(item: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
            async with gate:
                symbol = str(item.get("symbol", ""))
                try:
                    scoring = await self._run_json_stage(
                        label="market-scoring",
                        stage="market-scoring",
                        task=self._build_market_scoring_task([item]),
                        trace_stage=False,
                    )
                    scored_item = scoring
                    if not isinstance(scored_item, dict):
                        raise TypeError("scored item must be a JSON object")
                    scored_symbol = str(scored_item.get("symbol", ""))
                    if scored_symbol != symbol:
                        raise ValueError(
                            f"expected symbol {symbol}, got {scored_symbol or 'missing'}"
                        )
                    expected_keys = {"symbol", "technical_score", "score_reasons", "risk_notes"}
                    actual_keys = set(scored_item.keys())
                    if actual_keys != expected_keys:
                        raise ValueError(
                            "market-scoring item keys mismatch: "
                            f"expected {sorted(expected_keys)}, got {sorted(actual_keys)}"
                        )

                    technical_score_raw = scored_item["technical_score"]
                    score_reasons_raw = scored_item["score_reasons"]
                    risk_notes_raw = scored_item["risk_notes"]

                    if not isinstance(technical_score_raw, int) or isinstance(technical_score_raw, bool):
                        raise ValueError("market-scoring technical_score must be an integer")
                    if not 0 <= technical_score_raw <= 100:
                        raise ValueError("market-scoring technical_score must be between 0 and 100")
                    if not isinstance(score_reasons_raw, list) or any(
                        not isinstance(reason, str) for reason in score_reasons_raw
                    ):
                        raise ValueError("market-scoring score_reasons must be an array of strings")
                    if not isinstance(risk_notes_raw, list) or any(
                        not isinstance(note, str) for note in risk_notes_raw
                    ):
                        raise ValueError("market-scoring risk_notes must be an array of strings")
                    return scored_item, None
                except Exception as exc:
                    return None, f"market scoring unavailable for {symbol}: {exc}"

        results = await asyncio.gather(*(_score_one(item) for item in scoring_items))
        scored_items = [scored for scored, _failure in results if scored is not None]
        partial_failures = [failure for _scored, failure in results if failure is not None]
        return scored_items, partial_failures

    async def _review_news_candidates(
        self,
        review_items: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        async def _review_one(item: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
            symbol = str(item.get("symbol", ""))
            try:
                reviewed_news = await self._run_json_stage(
                    label="news-filter",
                    stage="news-filter",
                    task=self._build_news_filter_task([item]),
                    trace_stage=False,
                )
                reviewed_items_raw = reviewed_news.get("items", [])
                if not isinstance(reviewed_items_raw, list):
                    raise ValueError("news-filter items must be an array")

                validated_items: list[dict[str, Any]] = []
                for reviewed_item in reviewed_items_raw:
                    if not isinstance(reviewed_item, dict):
                        raise TypeError("news-filter item must be a JSON object")
                    reviewed_symbol = str(reviewed_item.get("symbol", ""))
                    if reviewed_symbol != symbol:
                        raise ValueError(
                            f"expected symbol {symbol}, got {reviewed_symbol or 'missing'}"
                        )
                    expected_keys = {"symbol", "name", "allowed", "risk_notes"}
                    actual_keys = set(reviewed_item.keys())
                    if actual_keys != expected_keys:
                        raise ValueError(
                            "news-filter item keys mismatch: "
                            f"expected {sorted(expected_keys)}, got {sorted(actual_keys)}"
                        )

                    name_raw = reviewed_item["name"]
                    allowed_raw = reviewed_item["allowed"]
                    risk_notes_raw = reviewed_item["risk_notes"]

                    if not isinstance(name_raw, str):
                        raise ValueError("news-filter name must be a string")
                    if not isinstance(allowed_raw, bool):
                        raise ValueError("news-filter allowed must be a boolean")
                    if not isinstance(risk_notes_raw, list) or any(
                        not isinstance(note, str) for note in risk_notes_raw
                    ):
                        raise ValueError("news-filter risk_notes must be an array of strings")
                    validated_items.append(reviewed_item)

                partial_failures_raw = reviewed_news.get("partial_failures", [])
                if not isinstance(partial_failures_raw, list) or any(
                    not isinstance(failure, str) for failure in partial_failures_raw
                ):
                    raise ValueError("news-filter partial_failures must be an array of strings")
                return validated_items, [str(failure) for failure in partial_failures_raw]
            except Exception as exc:
                return [], [f"news-filter unavailable for {symbol}: {exc}"]

        reviewed_groups = await asyncio.gather(*(_review_one(item) for item in review_items))
        reviewed_items: list[dict[str, Any]] = []
        partial_failures: list[str] = []
        for group_items, group_failures in reviewed_groups:
            reviewed_items.extend(group_items)
            partial_failures.extend(group_failures)
        return reviewed_items, partial_failures

    async def _prepare_news_filter_inputs(
        self,
        symbols: list[dict[str, Any]],
        trade_date: date | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        review_items: list[dict[str, Any]] = []
        auto_allowed_items: list[dict[str, Any]] = []

        if self.news_data is None:
            for symbol in symbols:
                review_items.append(
                    {
                        "symbol": symbol.get("symbol", ""),
                        "name": symbol.get("name", ""),
                        "has_negative_candidates": False,
                        "candidate_articles": [],
                    }
                )
            return review_items, auto_allowed_items

        async def _prepare_one(symbol: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            symbol_code = str(symbol.get("symbol", ""))
            symbol_name = str(symbol.get("name", ""))
            try:
                raw_articles = await asyncio.to_thread(
                    self.news_data.get_news,
                    symbol_code,
                    self.lookback_days,
                    trade_date,
                    symbol_name,
                )
            except Exception as exc:
                display_name = f"{symbol_code} ({symbol_name})" if symbol_name else symbol_code
                message = f"news data unavailable for {display_name}: {exc}"
                return None, {
                    "symbol": symbol_code,
                    "name": symbol_name,
                    "allowed": True,
                    "risk_notes": [message],
                }

            prescreened = prescreen_negative_news(symbol_code, adapt_news_articles(raw_articles))
            if not prescreened.has_negative_candidates:
                return None, {
                    "symbol": symbol_code,
                    "name": symbol_name,
                    "allowed": True,
                    "risk_notes": [],
                }

            return {
                "symbol": symbol_code,
                "name": symbol_name,
                "has_negative_candidates": True,
                "candidate_articles": [
                    self._candidate_article_to_dict(article)
                    for article in prescreened.candidate_articles
                ],
            }, None

        results = await asyncio.gather(*(_prepare_one(symbol) for symbol in symbols))
        for review_item, auto_allowed_item in results:
            if review_item is not None:
                review_items.append(review_item)
            if auto_allowed_item is not None:
                auto_allowed_items.append(auto_allowed_item)

        return review_items, auto_allowed_items

    async def _prepare_market_scoring_inputs(
        self,
        symbols: list[str],
        trade_date: date | None = None,
    ) -> list[dict[str, Any]]:
        scoring_items: list[dict[str, Any]] = []

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
                    message = (
                        f"technical data unavailable for {symbol}: "
                        "snapshot missing from batch response"
                    )
                    technical_snapshot = self._build_unavailable_technical_snapshot(symbol, message)

            scoring_snapshot = self._flatten_scoring_snapshot(technical_snapshot)
            scoring_items.append(
                {
                    "symbol": symbol,
                    "technical_snapshot": scoring_snapshot,
                }
            )

        return scoring_items

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
            "summary": article.summary,
            "source": article.source,
            "candidate_categories": list(article.candidate_categories),
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
        del screened
        selected: list[SelectedStockReport] = []
        news_by_symbol = {str(item["symbol"]): item for item in news_items if item.get("symbol")}

        for scoring_raw in scoring_items:
            scoring = ScoredStock(
                symbol=str(scoring_raw["symbol"]),
                technical_score=int(scoring_raw["technical_score"]),
                score_reasons=[str(reason) for reason in scoring_raw.get("score_reasons", [])],
                risk_notes=[str(note) for note in scoring_raw.get("risk_notes", [])],
            )
            news_raw = news_by_symbol.get(scoring.symbol, {})
            selected.append(
                SelectedStockReport(
                    symbol=scoring.symbol,
                    strategy_name=strategy_name,
                    screen_pass_reasons=[],
                    technical_score=scoring.technical_score,
                    score_reasons=scoring.score_reasons,
                    risk_notes=[
                        *[str(note) for note in news_raw.get("risk_notes", [])],
                        *scoring.risk_notes,
                    ],
                    report_date=trade_date,
                )
            )

        selected.sort(key=lambda item: (-item.technical_score, item.symbol))
        return selected[:10]
