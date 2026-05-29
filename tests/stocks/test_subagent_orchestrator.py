from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from nanobot.stocks.orchestrator import StockSelectionSubagentOrchestrator
from nanobot.stocks.service import DailySelectionServiceError


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
              {"symbol":"600001","allowed":true,"negative_news_flags":[],"risk_notes":[]}
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
    assert report.trade_date == date(2026, 5, 26)
    assert [item.symbol for item in report.selected_stocks] == ["600001"]
    assert report.selected_stocks[0].technical_score == 91
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
async def test_orchestrator_requires_supported_strategy() -> None:
    orchestrator = StockSelectionSubagentOrchestrator(executor=_FakeExecutor(responses=[]))

    with pytest.raises(DailySelectionServiceError, match="unknown strategy"):
        await orchestrator.run_daily_stock_selection("missing", date(2026, 5, 26))


def test_orchestrator_stage_prompts_define_roles_contracts_and_fail_safes() -> None:
    orchestrator = StockSelectionSubagentOrchestrator(executor=_FakeExecutor(responses=[]))

    screening_prompt = orchestrator._build_stock_screening_task("B1", date(2026, 5, 26))
    news_prompt = orchestrator._build_news_filter_task(["600001"])
    scoring_prompt = orchestrator._build_market_scoring_task(["600001"])
    summary_prompt = orchestrator._build_report_summary_task(["600001"])

    assert "Task: Execute the stock screening run" in screening_prompt
    assert "trade_date=2026-05-26" in screening_prompt
    assert "strategy=B1" in screening_prompt
    assert "strategy_skill_path=" not in screening_prompt
    assert '"screened_count": 5300' in screening_prompt

    assert "Task: Assess recent material negative news risk" in news_prompt
    assert '"partial_failures":[]' in news_prompt
    assert "symbols=" in news_prompt

    assert "Task: Score the supplied candidate symbols" in scoring_prompt
    assert '"technical_score":90' in scoring_prompt
    assert "symbols=" in scoring_prompt

    assert "Task: Produce the final report metadata" in summary_prompt
    assert '"global_risk_disclaimer"' in summary_prompt
    assert "selected=" in summary_prompt
    assert "excluded=" not in summary_prompt


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

    assert "Role:" not in screening_prompt
    assert "Output must be valid JSON only." not in screening_prompt
    assert "Return JSON only." not in screening_prompt


def test_orchestrator_stage_system_prompts_define_hard_constraints() -> None:
    orchestrator = StockSelectionSubagentOrchestrator(executor=_FakeExecutor(responses=[]))

    screening_system = orchestrator._build_stock_screening_system_prompt()
    news_system = orchestrator._build_news_filter_system_prompt()
    scoring_system = orchestrator._build_market_scoring_system_prompt()
    summary_system = orchestrator._build_report_summary_system_prompt()

    assert "You are a specialized A-share screening analyst." in screening_system
    assert "Fetch the full A-share stock universe from the MCP server" in screening_system
    assert "mapped strategy skill" not in screening_system
    assert "workspace skills" in screening_system

    assert "You are a specialized A-share risk news analyst." in news_system
    assert "If news is unavailable" in news_system

    assert "You are a specialized A-share technical scoring analyst." in scoring_system
    assert "technical_score must be an integer from 0 to 100" in scoring_system

    assert "You are a specialized A-share report summarizer." in summary_system
    assert "Do not invent extra fields" in summary_system

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
