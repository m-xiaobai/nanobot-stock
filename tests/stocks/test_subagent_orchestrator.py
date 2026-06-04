from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest

from nanobot.stocks.orchestrator import StockSelectionSubagentOrchestrator
from nanobot.stocks.service import DailySelectionServiceError, NewsArticle


class _FakeExecutor:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, str | None]] = []

    async def run_inline(
        self,
        *,
        task: str,
        label: str,
        temperature: float | None = None,
        extra_system_prompt: str | None = None,
    ) -> str:
        del temperature
        self.calls.append((label, task, extra_system_prompt))
        if not self._responses:
            raise AssertionError("unexpected subagent invocation")
        return self._responses.pop(0)


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


class _FakeTechnicalAdapter:
    def __init__(self, snapshots_by_symbol: dict[str, dict[str, Any] | Exception]) -> None:
        self._snapshots_by_symbol = snapshots_by_symbol
        self.calls: list[tuple[list[str], int, object | None]] = []

    def get_technical_snapshot(
        self,
        symbols: list[str],
        lookback_days: int,
        anchor_date: object | None = None,
    ) -> dict[str, dict[str, Any]]:
        self.calls.append((list(symbols), lookback_days, anchor_date))
        snapshots: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            result = self._snapshots_by_symbol.get(symbol)
            if isinstance(result, Exception):
                raise result
            if result is not None:
                snapshots[symbol] = dict(result)
        return snapshots


@pytest.mark.asyncio
async def test_orchestrator_runs_four_subagent_stages_and_merges_report() -> None:
    executor = _FakeExecutor(
        responses=[
            """
            {"items":[
              {"symbol":"600001","strategy_name":"B1",
               "screen_pass_reasons":["close broke above the recent range high"],"risk_notes":[]}
            ],
            "screened_count": 5300,
            "passed_count": 1,
            "screening_notes": ["screening completed with full market coverage"]}
            """,
            """
            {"items":[
              {"symbol":"600001","allowed":true,"decision":"PASS","matched_categories":[],"negative_news_flags":[],"risk_notes":[],"evidence":[]}
            ],"partial_failures":[]}
            """,
            """
            {"items":[
              {"symbol":"600001","technical_score":91,
               "score_reasons":["trend is above the short and medium moving averages"],
               "risk_notes":["watch for next-day follow-through"]}
            ]}
            """,
            """
            {"summary":"Selected candidates: 600001.",
             "global_risk_disclaimer":"For research use only."}
            """,
        ]
    )
    orchestrator = StockSelectionSubagentOrchestrator(executor=executor, screening_only=False)

    report = await orchestrator.run_daily_stock_selection("B1", date(2026, 5, 26))

    assert [label for label, _task, _system in executor.calls] == [
        "stock-screening",
        "news-filter",
        "market-scoring",
        "report-summary",
    ]
    market_scoring_calls = [task for label, task, _system in executor.calls if label == "market-scoring"]
    assert len(market_scoring_calls) == 1
    assert market_scoring_calls[0].count('"technical_snapshot"') == 1
    assert report.trade_date == date(2026, 5, 26)
    assert [item.symbol for item in report.selected_stocks] == ["600001"]
    assert report.selected_stocks[0].technical_score == 91
    assert report.selected_stocks[0].screen_pass_reasons == []
    assert report.selected_stocks[0].negative_news_flags == []
    assert report.selected_stocks[0].risk_notes == ["watch for next-day follow-through"]
    assert report.summary.startswith("Selected candidates")
    assert report.global_risk_disclaimer == "For research use only."


@pytest.mark.asyncio
async def test_orchestrator_rejects_invalid_subagent_json() -> None:
    executor = _FakeExecutor(responses=["not-json"])
    orchestrator = StockSelectionSubagentOrchestrator(executor=executor)

    with pytest.raises(DailySelectionServiceError, match="stock-screening"):
        await orchestrator.run_daily_stock_selection("B1", date(2026, 5, 26))


@pytest.mark.asyncio
async def test_orchestrator_defaults_to_screening_only_mode() -> None:
    executor = _FakeExecutor(
        responses=[
            """
            {"items":[
              {"symbol":"600001","strategy_name":"B1",
               "screen_pass_reasons":["close broke above the recent range high"],"risk_notes":[]},
              {"symbol":"600002","strategy_name":"B1",
               "screen_pass_reasons":["volume expanded versus the recent average"],"risk_notes":["needs manual review"]}
            ]}
            """
        ]
    )
    orchestrator = StockSelectionSubagentOrchestrator(executor=executor)

    report = await orchestrator.run_daily_stock_selection("B1", date(2026, 5, 26))

    assert [label for label, _task, _system in executor.calls] == ["stock-screening"]
    assert [item.symbol for item in report.selected_stocks] == ["600001", "600002"]
    assert report.selected_stocks[0].technical_score == 0
    assert report.selected_stocks[1].risk_notes == ["needs manual review"]
    assert report.summary == "Screening-only mode: 2 candidate(s) passed stock-screening."
    assert report.global_risk_disclaimer == "For research use only. This screening-only report is not investment advice."
    assert report.partial_failures == ["screening_only mode enabled; skipped news-filter, market-scoring, report-summary"]


@pytest.mark.asyncio
async def test_orchestrator_can_stop_after_news_filter_stage() -> None:
    executor = _FakeExecutor(
        responses=[
            """
            {"items":[
              {"symbol":"600001","strategy_name":"B1",
               "screen_pass_reasons":["close broke above the recent range high"],"risk_notes":[]},
              {"symbol":"000001","strategy_name":"B1",
               "screen_pass_reasons":["volume expanded versus the recent average"],"risk_notes":["watch disclosure follow-up"]}
            ]}
            """,
            """
            {"items":[
              {"symbol":"600001","allowed":true,"decision":"PASS","matched_categories":[],"negative_news_flags":[],"risk_notes":[],"evidence":[]},
              {"symbol":"000001","allowed":false,"decision":"REJECT","matched_categories":["regulatory investigation or administrative penalty"],
               "negative_news_flags":["CSRC investigation"],"risk_notes":["recent material negative news within 3 days"],"evidence":[]}
            ],"partial_failures":[]}
            """,
        ]
    )
    orchestrator = StockSelectionSubagentOrchestrator(
        executor=executor,
        screening_only=False,
        news_filter_only=True,
    )

    report = await orchestrator.run_daily_stock_selection("B1", date(2026, 5, 26))

    assert [label for label, _task, _system in executor.calls] == [
        "stock-screening",
        "news-filter",
    ]
    assert [item.symbol for item in report.selected_stocks] == ["600001"]
    assert report.selected_stocks[0].technical_score == 0
    assert report.selected_stocks[0].negative_news_flags == []
    assert report.summary == "News-filter-only mode: 1 candidate(s) passed stock-screening and news-filter."
    assert report.global_risk_disclaimer == "For research use only. This news-filter-only report is not investment advice."
    assert report.partial_failures == ["news_filter_only mode enabled; skipped market-scoring, report-summary"]


@pytest.mark.asyncio
async def test_orchestrator_requires_supported_strategy() -> None:
    orchestrator = StockSelectionSubagentOrchestrator(executor=_FakeExecutor(responses=[]))

    with pytest.raises(DailySelectionServiceError, match="unknown strategy"):
        await orchestrator.run_daily_stock_selection("missing", date(2026, 5, 26))


def test_orchestrator_stage_prompts_define_roles_contracts_and_fail_safes() -> None:
    orchestrator = StockSelectionSubagentOrchestrator(executor=_FakeExecutor(responses=[]))

    screening_prompt = orchestrator._build_stock_screening_task("B1", date(2026, 5, 26))
    news_prompt = orchestrator._build_news_filter_task(
        [{"symbol": "600001", "candidate_articles": []}]
    )
    scoring_prompt = orchestrator._build_market_scoring_task(["600001"])
    summary_prompt = orchestrator._build_report_summary_task(["600001"])

    assert "Task: Execute the stock screening run" in screening_prompt
    assert "trade_date=2026-05-26" in screening_prompt
    assert "strategy=B1" in screening_prompt
    assert "strategy_skill_path=" not in screening_prompt
    assert '"screened_count": 5300' in screening_prompt

    assert "Task: Assess recent material negative news risk" in news_prompt
    assert "lookback_days=3" in news_prompt
    assert '"partial_failures":[]' in news_prompt
    assert "items=" in news_prompt
    assert '"candidate_articles"' in news_prompt
    assert '"decision":"PASS"' in news_prompt

    assert "Task: Score the supplied candidate items" in scoring_prompt
    assert "Apply the system scoring rubric to each item" in scoring_prompt
    assert '"technical_score":90' in scoring_prompt
    assert "items=" in scoring_prompt

    assert "Task: Produce the final report metadata" in summary_prompt
    assert '"global_risk_disclaimer"' in summary_prompt
    assert "selected=" in summary_prompt
    assert "excluded=" not in summary_prompt


def test_merge_stage_outputs_uses_only_market_scoring_and_keeps_top_ten() -> None:
    scoring_items = [
        {
            "symbol": f"{600000 + index:06d}",
            "technical_score": score,
            "score_reasons": [f"reason-{score}"],
            "risk_notes": [f"risk-{score}"],
        }
        for index, score in enumerate([55, 92, 71, 88, 63, 77, 84, 96, 59, 81, 67, 90], start=1)
    ]

    selected = StockSelectionSubagentOrchestrator._merge_stage_outputs(
        strategy_name="B1",
        trade_date=date(2026, 5, 26),
        screened=[
            {"symbol": "placeholder", "strategy_name": "B1", "screen_pass_reasons": [], "risk_notes": []}
        ],
        news_items=[
            {"symbol": "placeholder", "allowed": True, "negative_news_flags": [], "risk_notes": []}
        ],
        scoring_items=scoring_items,
    )

    assert [item.symbol for item in selected] == [
        "600008",
        "600002",
        "600012",
        "600004",
        "600007",
        "600010",
        "600006",
        "600003",
        "600011",
        "600005",
    ]
    assert [item.technical_score for item in selected] == [96, 92, 90, 88, 84, 81, 77, 71, 67, 63]
    assert all(item.screen_pass_reasons == [] for item in selected)
    assert all(item.negative_news_flags == [] for item in selected)


@pytest.mark.asyncio
async def test_orchestrator_scores_each_allowed_symbol_in_a_separate_market_scoring_call() -> None:
    executor = _FakeExecutor(
        responses=[
            """
            {"items":[
              {"symbol":"600001","strategy_name":"B1",
               "screen_pass_reasons":["close broke above the recent range high"],"risk_notes":[]},
              {"symbol":"000001","strategy_name":"B1",
               "screen_pass_reasons":["volume expanded versus the recent average"],"risk_notes":[]}
            ]}
            """,
            """
            {"items":[
              {"symbol":"600001","allowed":true,"decision":"PASS","matched_categories":[],"negative_news_flags":[],"risk_notes":[],"evidence":[]},
              {"symbol":"000001","allowed":true,"decision":"PASS","matched_categories":[],"negative_news_flags":[],"risk_notes":[],"evidence":[]}
            ],"partial_failures":[]}
            """,
            """
            {"items":[
              {"symbol":"600001","technical_score":91,
               "score_reasons":["trend is above the short and medium moving averages"],
               "risk_notes":["watch for next-day follow-through"]}
            ]}
            """,
            """
            {"items":[
              {"symbol":"000001","technical_score":83,
               "score_reasons":["volume confirms the move"],
               "risk_notes":[]}
            ]}
            """,
            """
            {"summary":"Selected candidates: 600001, 000001.",
             "global_risk_disclaimer":"For research use only."}
            """,
        ]
    )
    orchestrator = StockSelectionSubagentOrchestrator(
        executor=executor,
        screening_only=False,
        technical_data=_FakeTechnicalAdapter(
            {
                "600001": {"symbol": "600001", "close": 12.36, "data_status": "ok"},
                "000001": {"symbol": "000001", "close": 9.18, "data_status": "ok"},
            }
        ),
    )

    report = await orchestrator.run_daily_stock_selection("B1", date(2026, 5, 26))

    market_scoring_calls = [task for label, task, _system in executor.calls if label == "market-scoring"]

    assert len(market_scoring_calls) == 2
    assert '"symbol": "600001"' in market_scoring_calls[0]
    assert '"symbol": "000001"' not in market_scoring_calls[0]
    assert '"symbol": "000001"' in market_scoring_calls[1]
    assert '"symbol": "600001"' not in market_scoring_calls[1]
    assert [item.symbol for item in report.selected_stocks] == ["600001", "000001"]


@pytest.mark.asyncio
async def test_orchestrator_skips_single_symbol_market_scoring_failures_and_records_partial_failures() -> None:
    executor = _FakeExecutor(
        responses=[
            """
            {"items":[
              {"symbol":"600001","strategy_name":"B1",
               "screen_pass_reasons":["close broke above the recent range high"],"risk_notes":[]},
              {"symbol":"000001","strategy_name":"B1",
               "screen_pass_reasons":["volume expanded versus the recent average"],"risk_notes":[]}
            ]}
            """,
            """
            {"items":[
              {"symbol":"600001","allowed":true,"decision":"PASS","matched_categories":[],"negative_news_flags":[],"risk_notes":[],"evidence":[]},
              {"symbol":"000001","allowed":true,"decision":"PASS","matched_categories":[],"negative_news_flags":[],"risk_notes":[],"evidence":[]}
            ],"partial_failures":[]}
            """,
            """
            {"items":[
              {"symbol":"600001","technical_score":91,
               "score_reasons":["trend is above the short and medium moving averages"],
               "risk_notes":["watch for next-day follow-through"]}
            ]}
            """,
            """
            {"items":[]}
            """,
            """
            {"summary":"Selected candidates: 600001.",
             "global_risk_disclaimer":"For research use only."}
            """,
        ]
    )
    orchestrator = StockSelectionSubagentOrchestrator(
        executor=executor,
        screening_only=False,
        technical_data=_FakeTechnicalAdapter(
            {
                "600001": {"symbol": "600001", "close": 12.36, "data_status": "ok"},
                "000001": {"symbol": "000001", "close": 9.18, "data_status": "ok"},
            }
        ),
    )

    report = await orchestrator.run_daily_stock_selection("B1", date(2026, 5, 26))

    assert [item.symbol for item in report.selected_stocks] == ["600001"]
    assert report.partial_failures == ["market scoring unavailable for 000001: expected exactly one scored item"]


def test_orchestrator_stage_prompts_are_loaded_from_templates() -> None:
    orchestrator = StockSelectionSubagentOrchestrator(executor=_FakeExecutor(responses=[]))

    screening_prompt = orchestrator._build_stock_screening_task("B1", date(2026, 5, 26))
    screening_system = orchestrator._build_stock_screening_system_prompt()

    assert "Task: Execute the stock screening run" in screening_prompt
    assert "You are a specialized A-share screening analyst." in screening_system
    assert "{{" not in screening_prompt
    assert "{{" not in screening_system


def test_orchestrator_task_prompts_keep_hard_rules_out_of_user_layer() -> None:
    orchestrator = StockSelectionSubagentOrchestrator(executor=_FakeExecutor(responses=[]))

    screening_prompt = orchestrator._build_stock_screening_task("B1", date(2026, 5, 26))
    scoring_prompt = orchestrator._build_market_scoring_task(
        [{"symbol": "600001", "technical_snapshot": {"close": 12.3, "ma5": 12.1}}]
    )

    assert "Role:" not in screening_prompt
    assert "Output must be valid JSON only." not in screening_prompt
    assert "Return JSON only." not in screening_prompt
    assert "趋势结构" not in scoring_prompt
    assert "0-25" not in scoring_prompt
    assert "FILTER_OUT" not in scoring_prompt


def test_orchestrator_stage_system_prompts_define_hard_constraints() -> None:
    orchestrator = StockSelectionSubagentOrchestrator(executor=_FakeExecutor(responses=[]))

    screening_system = orchestrator._build_stock_screening_system_prompt()
    news_system = orchestrator._build_news_filter_system_prompt()
    scoring_system = orchestrator._build_market_scoring_system_prompt()
    summary_system = orchestrator._build_report_summary_system_prompt()

    assert "You are a specialized A-share screening analyst." in screening_system
    assert "Source-of-truth rules:" in screening_system
    assert "dedicated MCP screening tool" in screening_system
    assert "Completion rule:" in screening_system

    assert "You are a specialized A-share risk news analyst." in news_system
    assert "If news is unavailable" in news_system
    assert "only review the supplied candidate articles" in news_system

    assert "You are a specialized A-share technical scoring analyst." in scoring_system
    assert "technical_score must be an integer from 0 to 100" in scoring_system
    assert "Scoring rubric:" in scoring_system
    assert "trend structure: 0-25" in scoring_system
    assert "range position: 0-10" in scoring_system
    assert "volume-price confirmation: 0-20" in scoring_system
    assert "short-term momentum: 0-10" in scoring_system
    assert "MACD: 0-20" in scoring_system
    assert "RSI: 0-10" in scoring_system
    assert "risk penalty: 0 to -15" in scoring_system
    assert "technical_score = trend + position + volume_price + momentum + macd + rsi + risk_penalty" in scoring_system
    assert "Decision bands:" in scoring_system
    assert "<40 => FILTER_OUT" in scoring_system
    assert "40-54 => WEAK_PASS" in scoring_system
    assert ">=55 => PASS" in scoring_system
    assert "Do not invent your own scoring rubric" in scoring_system

    assert "You are a specialized A-share report summarizer." in summary_system
    assert "Do not invent extra fields" in summary_system


def test_orchestrator_prescreens_news_and_only_escalates_candidate_hits() -> None:
    news_data = _FakeNewsAdapter(
        {
            "600001": [
                NewsArticle(
                    title="600001收到证监会立案告知书",
                    published_at="2026-06-01T09:30:00+08:00",
                    summary="公司涉嫌信息披露违法违规，被证监会立案调查。",
                )
            ],
            "000001": [
                NewsArticle(
                    title="000001接待机构调研",
                    published_at="2026-06-01T09:30:00+08:00",
                    summary="本次机构调研围绕新品发布和产能规划展开。",
                )
            ],
        }
    )
    orchestrator = StockSelectionSubagentOrchestrator(
        executor=_FakeExecutor(responses=[]),
        news_data=news_data,
    )

    review_items, auto_allowed_items, partial_failures = orchestrator._prepare_news_filter_inputs(
        ["600001", "000001"],
        trade_date=date(2026, 5, 26),
    )

    assert partial_failures == []
    assert review_items == [
        {
            "symbol": "600001",
            "has_negative_candidates": True,
            "candidate_articles": [
                {
                    "date": "2026-06-01",
                    "title": "600001收到证监会立案告知书",
                    "matched_keywords": ["立案", "证监会", "调查"],
                    "candidate_categories": ["regulatory investigation or administrative penalty"],
                    "rule_severity": "high",
                }
            ],
        }
    ]
    assert auto_allowed_items == [
        {
            "symbol": "000001",
            "allowed": True,
            "decision": "PASS",
            "matched_categories": [],
            "negative_news_flags": [],
            "risk_notes": [],
            "evidence": [],
        }
    ]
    assert news_data.calls == [
        ("600001", 3, date(2026, 5, 26)),
        ("000001", 3, date(2026, 5, 26)),
    ]

@pytest.mark.asyncio
async def test_orchestrator_accepts_b2_strategy() -> None:
    executor = _FakeExecutor(
        responses=[
            """
            {"items":[
              {"symbol":"600001","strategy_name":"B2",
               "screen_pass_reasons":["short-term moving averages are aligned above medium-term support"],"risk_notes":[]}
            ]}
            """
        ]
    )
    orchestrator = StockSelectionSubagentOrchestrator(executor=executor)

    report = await orchestrator.run_daily_stock_selection("B2", date(2026, 5, 26))

    assert report.strategy_name == "B2"
    assert [item.symbol for item in report.selected_stocks] == ["600001"]


def test_orchestrator_prepares_market_scoring_inputs_from_technical_adapter() -> None:
    orchestrator = StockSelectionSubagentOrchestrator(
        executor=_FakeExecutor(responses=[]),
        technical_data=_FakeTechnicalAdapter(
            {
                "600001": {
                    "symbol": "600001",
                    "close": 12.36,
                    "ma5": 11.98,
                    "data_status": "ok",
                }
            },
        ),
    )

    items, failures = asyncio.run(
        orchestrator._prepare_market_scoring_inputs(["600001"], date(2026, 5, 26))
    )

    assert failures == []
    assert items == [
        {
            "symbol": "600001",
            "technical_snapshot": {
                "symbol": "600001",
                "close": 12.36,
                "ma5": 11.98,
                "data_status": "ok",
            },
        }
    ]


def test_orchestrator_prepares_market_scoring_inputs_from_batch_technical_adapter() -> None:
    orchestrator = StockSelectionSubagentOrchestrator(
        executor=_FakeExecutor(responses=[]),
        technical_data=_FakeTechnicalAdapter(
            {
                "600001": {
                    "symbol": "600001",
                    "close": 12.36,
                    "data_status": "ok",
                },
                "000001": {
                    "symbol": "000001",
                    "close": 9.18,
                    "data_status": "ok",
                },
            },
        ),
    )

    items, failures = asyncio.run(
        orchestrator._prepare_market_scoring_inputs(["600001", "000001"], date(2026, 5, 26))
    )

    assert failures == []
    assert items == [
        {
            "symbol": "600001",
            "technical_snapshot": {
                "symbol": "600001",
                "close": 12.36,
                "data_status": "ok",
            },
        },
        {
            "symbol": "000001",
            "technical_snapshot": {
                "symbol": "000001",
                "close": 9.18,
                "data_status": "ok",
            },
        },
    ]
    assert orchestrator.technical_data.calls == [
        (["600001", "000001"], 60, date(2026, 5, 26))
    ]


def test_orchestrator_marks_missing_batch_snapshot_as_unavailable() -> None:
    orchestrator = StockSelectionSubagentOrchestrator(
        executor=_FakeExecutor(responses=[]),
        technical_data=_FakeTechnicalAdapter(
            {
                "600001": {
                    "symbol": "600001",
                    "close": 12.36,
                    "data_status": "ok",
                }
            },
        ),
    )

    items, failures = asyncio.run(
        orchestrator._prepare_market_scoring_inputs(["600001", "000001"], date(2026, 5, 26))
    )

    assert failures == ["technical data unavailable for 000001: snapshot missing from batch response"]
    assert items == [
        {
            "symbol": "600001",
            "technical_snapshot": {
                "symbol": "600001",
                "close": 12.36,
                "data_status": "ok",
            },
        },
        {
            "symbol": "000001",
            "technical_snapshot": {
                "symbol": "000001",
                "data_status": "unavailable",
                "reason": "technical data unavailable for 000001: snapshot missing from batch response",
            },
        },
    ]


def test_orchestrator_marks_missing_technical_adapter_as_unavailable() -> None:
    orchestrator = StockSelectionSubagentOrchestrator(executor=_FakeExecutor(responses=[]))

    items, failures = asyncio.run(
        orchestrator._prepare_market_scoring_inputs(["600001"], date(2026, 5, 26))
    )

    assert failures == []
    assert items == [
        {
            "symbol": "600001",
            "technical_snapshot": {
                "symbol": "600001",
                "data_status": "unavailable",
                "reason": "technical data adapter not configured",
            },
        }
    ]


@pytest.mark.asyncio
async def test_orchestrator_accepts_expanded_news_filter_contract_and_preserves_failures() -> None:
    executor = _FakeExecutor(
        responses=[
            """
            {"items":[
              {"symbol":"600001","strategy_name":"B1",
               "screen_pass_reasons":["close broke above the recent range high"],"risk_notes":[]},
              {"symbol":"000001","strategy_name":"B1",
               "screen_pass_reasons":["volume expanded versus the recent average"],"risk_notes":[]}
            ]}
            """,
            """
            {"items":[
              {"symbol":"600001","allowed":true,"decision":"REVIEW",
               "matched_categories":["debt or litigation risk"],
               "negative_news_flags":["minor litigation review"],
               "risk_notes":["recent negative headlines need manual attention"],
               "evidence":[{"date":"2026-06-01","title":"诉讼已受理","category":"debt or litigation risk","severity":"medium"}]},
              {"symbol":"000001","allowed":false,"decision":"REJECT",
               "matched_categories":["regulatory investigation or administrative penalty"],
               "negative_news_flags":["CSRC investigation"],
               "risk_notes":["recent material negative news within 3 days"],
               "evidence":[{"date":"2026-06-01","title":"收到立案告知书","category":"regulatory investigation or administrative penalty","severity":"high"}]}
            ],
            "partial_failures":["news source timeout for 000001"]}
            """,
            """
            {"items":[
              {"symbol":"600001","technical_score":88,
               "score_reasons":["trend is above the short and medium moving averages"],
               "risk_notes":[]}
            ]}
            """,
            """
            {"summary":"Selected candidates: 600001.",
             "global_risk_disclaimer":"For research use only."}
            """,
        ]
    )
    orchestrator = StockSelectionSubagentOrchestrator(executor=executor, screening_only=False)

    report = await orchestrator.run_daily_stock_selection("B1", date(2026, 5, 26))

    assert [item.symbol for item in report.selected_stocks] == ["600001"]
    assert report.selected_stocks[0].negative_news_flags == []
    assert report.selected_stocks[0].risk_notes == []
    assert report.partial_failures == ["news source timeout for 000001"]


@pytest.mark.asyncio
async def test_orchestrator_reviews_each_prescreen_hit_in_a_separate_news_filter_call() -> None:
    executor = _FakeExecutor(
        responses=[
            """
            {"items":[
              {"symbol":"600001","strategy_name":"B1",
               "screen_pass_reasons":["close broke above the recent range high"],"risk_notes":[]},
              {"symbol":"000001","strategy_name":"B1",
               "screen_pass_reasons":["volume expanded versus the recent average"],"risk_notes":[]}
            ]}
            """,
            """
            {"items":[
              {"symbol":"600001","allowed":true,"decision":"PASS",
               "matched_categories":["regulatory investigation or administrative penalty"],
               "negative_news_flags":["CSRC investigation"],
               "risk_notes":[],"evidence":[]}
            ],"partial_failures":[]}
            """,
            """
            {"items":[
              {"symbol":"000001","allowed":false,"decision":"REJECT",
               "matched_categories":["major reduction plan or lockup-expiry pressure"],
               "negative_news_flags":["major shareholder reduction plan"],
               "risk_notes":["recent material negative news within 3 days"],"evidence":[]}
            ],"partial_failures":[]}
            """,
            """
            {"items":[
              {"symbol":"600001","technical_score":87,
               "score_reasons":["trend is above the short and medium moving averages"],
               "risk_notes":[]}
            ]}
            """,
            """
            {"summary":"Selected candidates: 600001.",
             "global_risk_disclaimer":"For research use only."}
            """,
        ]
    )
    orchestrator = StockSelectionSubagentOrchestrator(
        executor=executor,
        screening_only=False,
        news_data=_FakeNewsAdapter(
            {
                "600001": [
                    NewsArticle(
                        title="600001收到证监会立案告知书",
                        published_at="2026-06-01T09:30:00+08:00",
                        summary="公司涉嫌信息披露违法违规，被证监会立案调查。",
                    )
                ],
                "000001": [
                    NewsArticle(
                        title="000001控股股东披露大额减持计划",
                        published_at="2026-06-01T09:30:00+08:00",
                        summary="控股股东拟在未来三个月内减持不超过总股本的5%。",
                    )
                ],
            }
        ),
    )

    report = await orchestrator.run_daily_stock_selection("B1", date(2026, 5, 26))

    news_filter_calls = [task for label, task, _system in executor.calls if label == "news-filter"]

    assert len(news_filter_calls) == 2
    assert '"symbol": "600001"' in news_filter_calls[0]
    assert '"symbol": "000001"' not in news_filter_calls[0]
    assert '"symbol": "000001"' in news_filter_calls[1]
    assert [item.symbol for item in report.selected_stocks] == ["600001"]
