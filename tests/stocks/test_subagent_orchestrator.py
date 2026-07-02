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
        self.calls: list[tuple[str, str, str | None, bool, bool, bool]] = []

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
    ) -> str:
        del temperature
        self.calls.append(
            (
                label,
                task,
                extra_system_prompt,
                allow_builtin_tools,
                allow_mcp_tools,
                use_lightweight_system_prompt,
            )
        )
        if not self._responses:
            raise AssertionError("unexpected subagent invocation")
        return self._responses.pop(0)


class _FakeNewsAdapter:
    def __init__(self, articles_by_symbol: dict[str, list[NewsArticle] | Exception]) -> None:
        self._articles_by_symbol = articles_by_symbol
        self.calls: list[tuple[str, int, object | None, str | None]] = []

    def get_news(
        self,
        symbol: str,
        lookback_days: int,
        anchor_date: object | None = None,
        name: str | None = None,
    ) -> list[NewsArticle]:
        self.calls.append((symbol, lookback_days, anchor_date, name))
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


class _FakeLangfuseSpan:
    def __init__(self, name: str, sink: list[tuple[str, str]]) -> None:
        self.name = name
        self._sink = sink

    def __enter__(self) -> "_FakeLangfuseSpan":
        self._sink.append(("enter", self.name))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._sink.append(("exit", self.name))


class _FakeLangfuseAttributes:
    def __init__(self, *, session_id: str | None, metadata: dict[str, Any], sink: list[tuple[str, Any]]) -> None:
        self.session_id = session_id
        self.metadata = metadata
        self._sink = sink

    def __enter__(self) -> "_FakeLangfuseAttributes":
        self._sink.append(("enter_attributes", {"session_id": self.session_id, "metadata": self.metadata}))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._sink.append(("exit_attributes", self.session_id))


class _FakeLangfuseClient:
    def __init__(self) -> None:
        self.events = _FAKE_LANGFUSE_EVENTS

    def start_as_current_observation(self, *, name: str, as_type: str):
        self.events.append(("start_observation", {"name": name, "as_type": as_type}))
        self.events.append(("start_span", name))
        return _FakeLangfuseSpan(name, self.events)

    def update_current_observation(self, **payload: Any) -> None:
        self.events.append(("update_current_observation", payload))


def _fake_propagate_attributes(*, metadata: dict[str, Any], session_id: str | None = None):
    return _FakeLangfuseAttributes(session_id=session_id, metadata=metadata, sink=_FAKE_LANGFUSE_EVENTS)


_FAKE_LANGFUSE_EVENTS: list[tuple[str, Any]] = []


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
              {"symbol":"600001","name":"Alpha Corp","allowed":true,"risk_notes":[]}
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

    assert [label for label, _task, _system, _builtin, _mcp, _light in executor.calls] == [
        "stock-screening",
        "news-filter",
        "market-scoring",
        "report-summary",
    ]
    market_scoring_calls = [
        (task, allow_builtin_tools, allow_mcp_tools, use_lightweight_system_prompt)
        for label, task, _system, allow_builtin_tools, allow_mcp_tools, use_lightweight_system_prompt in executor.calls
        if label == "market-scoring"
    ]
    news_filter_calls = [
        (task, allow_builtin_tools, allow_mcp_tools, use_lightweight_system_prompt)
        for label, task, _system, allow_builtin_tools, allow_mcp_tools, use_lightweight_system_prompt in executor.calls
        if label == "news-filter"
    ]
    assert len(news_filter_calls) == 1
    assert news_filter_calls[0][1] is False
    assert news_filter_calls[0][2] is False
    assert news_filter_calls[0][3] is True
    assert len(market_scoring_calls) == 1
    assert market_scoring_calls[0][0].count('"technical_snapshot"') == 1
    assert market_scoring_calls[0][1] is False
    assert market_scoring_calls[0][2] is False
    assert market_scoring_calls[0][3] is True
    assert report.trade_date == date(2026, 5, 26)
    assert [item.symbol for item in report.selected_stocks] == ["600001"]
    assert report.selected_stocks[0].technical_score == 91
    assert report.selected_stocks[0].screen_pass_reasons == []
    assert report.selected_stocks[0].risk_notes == ["watch for next-day follow-through"]
    assert report.summary.startswith("市场评分已完成")
    assert report.global_risk_disclaimer == "仅供研究参考，不构成任何投资建议。"


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
    assert all(item.screen_pass_reasons == [] for item in report.selected_stocks)
    assert all(item.risk_notes == [] for item in report.selected_stocks)
    assert report.selected_stocks[0].technical_score == 0
    assert report.summary == "Screening-only mode: 2 candidate(s) passed stock-screening."
    assert report.global_risk_disclaimer == "For research use only. This screening-only report is not investment advice."
    assert report.partial_failures == []


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
              {"symbol":"600001","name":"Alpha Corp","allowed":true,"risk_notes":[]},
              {"symbol":"000001","name":"Beta Bank","allowed":false,
               "risk_notes":["recent material negative news within 3 days"]}
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
    assert report.selected_stocks[0].screen_pass_reasons == []
    assert report.selected_stocks[0].technical_score == 0
    assert report.selected_stocks[0].risk_notes == []
    assert report.summary == "News-filter-only mode: 1 candidate(s) passed stock-screening and news-filter."
    assert report.global_risk_disclaimer == "For research use only. This news-filter-only report is not investment advice."
    assert report.partial_failures == ["news source timeout for 000001"]


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
    scoring_prompt = orchestrator._build_market_scoring_task(
        [{"symbol": "600001", "technical_snapshot": {"close": 12.3, "ma5": 12.1}}]
    )
    summary_prompt = orchestrator._build_report_summary_task(["600001"])

    assert "Task: Execute the stock screening run" in screening_prompt
    assert "trade_date=2026-05-26" in screening_prompt
    assert "strategy=B1" in screening_prompt
    assert "strategy_skill_path=" not in screening_prompt
    assert '"screened_count": 5300' in screening_prompt
    assert '"screen_pass_reasons"' not in screening_prompt
    assert '"risk_notes"' not in screening_prompt

    assert "Task: Assess recent material negative news risk" in news_prompt
    assert "lookback_days=3" in news_prompt
    assert '"partial_failures":[]' in news_prompt
    assert "items=" in news_prompt
    assert '"candidate_articles"' in news_prompt
    assert '"allowed":true' in news_prompt

    assert "技术评分" in scoring_prompt
    assert "items=" in scoring_prompt
    assert "Output contract" not in scoring_prompt
    assert "Apply the system scoring rubric" not in scoring_prompt

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
            {"symbol": "placeholder", "name": "Placeholder Corp", "allowed": True, "risk_notes": []}
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


@pytest.mark.asyncio
async def test_orchestrator_scores_each_allowed_symbol_in_individual_market_scoring_calls() -> None:
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

    market_scoring_calls = [task for label, task, *_rest in executor.calls if label == "market-scoring"]

    assert len(market_scoring_calls) == 2
    assert sum('"symbol": "600001"' in task for task in market_scoring_calls) == 1
    assert sum('"symbol": "000001"' in task for task in market_scoring_calls) == 1
    assert all(task.count('"symbol": "') == 2 for task in market_scoring_calls)
    assert [item.symbol for item in report.selected_stocks] == ["600001", "000001"]


@pytest.mark.asyncio
async def test_orchestrator_records_langfuse_session_and_stage_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    _FAKE_LANGFUSE_EVENTS.clear()
    executor = _FakeExecutor(
        responses=[
            """
            {"items":[
              {"symbol":"600001","strategy_name":"B1",
               "screen_pass_reasons":["close broke above the recent range high"],"risk_notes":[]}
            ]}
            """,
            """
            {"items":[
              {"symbol":"600001","technical_score":91,
               "score_reasons":["trend is above the short and medium moving averages"],
               "risk_notes":["watch for next-day follow-through"]}
            ]}
            """,
        ]
    )
    fake_client = _FakeLangfuseClient()
    monkeypatch.setattr("nanobot.stocks.orchestrator.get_client", lambda: fake_client)
    monkeypatch.setattr("nanobot.stocks.orchestrator.propagate_attributes", _fake_propagate_attributes)

    orchestrator = StockSelectionSubagentOrchestrator(
        executor=executor,
        screening_only=False,
        technical_data=_FakeTechnicalAdapter(
            {"600001": {"symbol": "600001", "close": 12.36, "data_status": "ok"}}
        ),
    )

    report = await orchestrator.run_daily_stock_selection("B1", date(2026, 5, 26))

    attribute_events = [payload for kind, payload in fake_client.events if kind == "enter_attributes"]
    assert len(attribute_events) == 3
    assert attribute_events[0]["session_id"].startswith("stock-selection:A:B1:2026-05-26:run-")
    assert attribute_events[0]["metadata"] == {
        "strategy_name": "B1",
        "trade_date": "2026-05-26",
        "market": "A",
    }
    assert attribute_events[1] == {
        "session_id": None,
        "metadata": {"stage": "stock-screening"},
    }
    assert attribute_events[2] == {
        "session_id": None,
        "metadata": {"stage": "market-scoring"},
    }

    started_observations = [payload for kind, payload in fake_client.events if kind == "start_observation"]
    assert all(item["as_type"] == "span" for item in started_observations)

    started_spans = [payload for kind, payload in fake_client.events if kind == "start_span"]
    assert started_spans == [
        "run_daily_stock_selection",
        "stock-screening",
        "prepare-market-scoring-inputs",
        "market-scoring",
        "merge-stage-outputs",
    ]
    observation_updates = [
        payload for kind, payload in fake_client.events if kind == "update_current_observation"
    ]
    assert observation_updates == [
        {
            "input": {
                "strategy_name": "B1",
                "trade_date": "2026-05-26",
                "screened_count": 1,
                "news_items_count": 0,
                "scoring_items": [
                    {
                        "symbol": "600001",
                        "technical_score": 91,
                        "score_reasons": ["trend is above the short and medium moving averages"],
                        "risk_notes": ["watch for next-day follow-through"],
                    }
                ],
            },
            "output": {
                "selected_count": 1,
                "selected_stocks": [
                    {
                        "symbol": "600001",
                        "technical_score": 91,
                        "score_reasons": ["trend is above the short and medium moving averages"],
                        "risk_notes": ["watch for next-day follow-through"],
                    }
                ],
            },
        }
    ]
    assert [item.symbol for item in report.selected_stocks] == ["600001"]


@pytest.mark.asyncio
async def test_orchestrator_ignores_langfuse_when_context_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _FAKE_LANGFUSE_EVENTS.clear()
    executor = _FakeExecutor(
        responses=[
            """
            {"items":[
              {"symbol":"600001","strategy_name":"B1",
               "screen_pass_reasons":["close broke above the recent range high"],"risk_notes":[]}
            ]}
            """,
            """
            {"items":[
              {"symbol":"600001","technical_score":91,
               "score_reasons":["trend is above the short and medium moving averages"],
               "risk_notes":["watch for next-day follow-through"]}
            ]}
            """,
        ]
    )
    monkeypatch.setattr("nanobot.stocks.orchestrator.get_client", None)
    monkeypatch.setattr("nanobot.stocks.orchestrator.propagate_attributes", None)

    orchestrator = StockSelectionSubagentOrchestrator(
        executor=executor,
        screening_only=False,
        technical_data=_FakeTechnicalAdapter(
            {"600001": {"symbol": "600001", "close": 12.36, "data_status": "ok"}}
        ),
    )

    report = await orchestrator.run_daily_stock_selection("B1", date(2026, 5, 26))

    assert [label for label, _task, _system, _builtin, _mcp, _light in executor.calls] == [
        "stock-screening",
        "market-scoring",
    ]
    assert _FAKE_LANGFUSE_EVENTS == []
    assert [item.symbol for item in report.selected_stocks] == ["600001"]


@pytest.mark.asyncio
async def test_orchestrator_skips_single_symbol_market_scoring_failures_and_returns_empty_partial_failures() -> None:
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
              {"symbol":"600001","technical_score":91,
               "score_reasons":["trend is above the short and medium moving averages"],
               "risk_notes":["watch for next-day follow-through"]}
            ]}
            """,
            """
            {"items":[]}
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
    assert report.partial_failures == [
        "market scoring unavailable for 000001: list index out of range"
    ]


@pytest.mark.asyncio
async def test_orchestrator_accepts_market_scoring_max_concurrency_setting() -> None:
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
        market_scoring_max_concurrency=2,
    )

    report = await orchestrator.run_daily_stock_selection("B1", date(2026, 5, 26))

    market_scoring_calls = [task for label, task, *_rest in executor.calls if label == "market-scoring"]

    assert len(market_scoring_calls) == 2
    assert sum('"symbol": "600001"' in task for task in market_scoring_calls) == 1
    assert sum('"symbol": "000001"' in task for task in market_scoring_calls) == 1
    assert [item.symbol for item in report.selected_stocks] == ["600001", "000001"]


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

    assert "你是一名专门分析 A 股风险新闻的分析师。" in news_system
    assert "如果提供的新闻上下文不可用、不完整" in news_system
    assert "不要抓取、假设或依赖候选文章之外的外部新闻" in news_system
    assert "背景性、持续性或存量风险" in news_system
    assert "优先保持 `allowed=true`，并尽量给出简短 `risk_notes`" in news_system
    assert "候选文章记录本身出现在回看窗口内" in news_system
    assert "不要仅因这篇文章最近出现就判定为近期新增重大负面事件" in news_system
    assert "评论、情绪分析、财富号/雪球等分析性表述" in news_system
    assert "不要把作者或平台的总结性判断直接改写为更确定的风险事实" in news_system
    assert "只用于记录候选文章中已出现、但不足以支持 `allowed=false` 的风险关注点" in news_system
    assert "如果上下文没有明显需要提示的风险点，可以输出空数组" in news_system

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


async def test_orchestrator_prescreens_news_and_escalates_articles_old_excludes_would_have_suppressed() -> None:
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
                    title="000001收到证监会监管函并披露机构调研纪要",
                    published_at="2026-06-01T09:30:00+08:00",
                    summary="公司披露监管函相关事项，并附机构调研纪要说明。",
                )
            ],
        }
    )
    orchestrator = StockSelectionSubagentOrchestrator(
        executor=_FakeExecutor(responses=[]),
        news_data=news_data,
    )

    review_items, auto_allowed_items = await orchestrator._prepare_news_filter_inputs(
        [
            {"symbol": "600001", "name": ""},
            {"symbol": "000001", "name": ""},
        ],
        trade_date=date(2026, 5, 26),
    )
    assert review_items == [
        {
            "symbol": "600001",
            "name": "",
            "has_negative_candidates": True,
            "candidate_articles": [
                {
                    "date": "2026-06-01",
                    "title": "600001收到证监会立案告知书",
                    "summary": "公司涉嫌信息披露违法违规，被证监会立案调查。",
                    "source": "东方财富网",
                    "candidate_categories": ["regulatory investigation or administrative penalty"],
                }
            ],
        },
        {
            "symbol": "000001",
            "name": "",
            "has_negative_candidates": True,
            "candidate_articles": [
                {
                    "date": "2026-06-01",
                    "title": "000001收到证监会监管函并披露机构调研纪要",
                    "summary": "公司收到监管函，同时披露机构调研纪要。",
                    "source": "东方财富网",
                    "candidate_categories": ["regulatory investigation or administrative penalty"],
                }
            ],
        }
    ]
    assert auto_allowed_items == []
    assert news_data.calls == [
        ("600001", 3, date(2026, 5, 26), "Alpha Corp"),
        ("000001", 3, date(2026, 5, 26), "Beta Bank"),
    ]


@pytest.mark.asyncio
async def test_orchestrator_skips_news_filter_llm_when_no_articles_are_found() -> None:
    news_data = _FakeNewsAdapter(
        {
            "600001": [],
        }
    )
    orchestrator = StockSelectionSubagentOrchestrator(
        executor=_FakeExecutor(responses=[]),
        news_data=news_data,
    )

    review_items, auto_allowed_items = await orchestrator._prepare_news_filter_inputs(
        [
            {"symbol": "600001", "name": "Alpha Corp"},
        ],
        trade_date=date(2026, 5, 26),
    )

    assert review_items == []
    assert auto_allowed_items == [
        {
            "symbol": "600001",
            "name": "Alpha Corp",
            "allowed": True,
            "risk_notes": [],
        }
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
              {"symbol":"600001","name":"Alpha Corp","allowed":true,
               "risk_notes":["recent negative headlines need manual attention"]},
              {"symbol":"000001","name":"Beta Bank","allowed":false,
               "risk_notes":["recent material negative news within 3 days"]}
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
    assert report.selected_stocks[0].risk_notes == []
    assert report.partial_failures == []


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
              {"symbol":"600001","name":"Alpha Corp","allowed":true,
               "risk_notes":[]}
            ],"partial_failures":[]}
            """,
            """
            {"items":[
              {"symbol":"000001","name":"Beta Bank","allowed":false,
               "risk_notes":["recent material negative news within 3 days"]}
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


@pytest.mark.asyncio
async def test_orchestrator_validates_news_filter_item_contract() -> None:
    executor = _FakeExecutor(
        responses=[
            """
            {"items":[
              {"symbol":"600001","strategy_name":"B1",
               "screen_pass_reasons":["close broke above the recent range high"],"risk_notes":[]}
            ]}
            """,
            """
            {"items":[
              {"symbol":"600001","allowed":true,"risk_notes":[]}
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

    assert report.selected_stocks == []
    assert report.partial_failures == [
        "news-filter unavailable for 600001: news-filter item keys mismatch: expected ['allowed', 'name', 'risk_notes', 'symbol'], got ['allowed', 'risk_notes', 'symbol']"
    ]


@pytest.mark.asyncio
async def test_orchestrator_public_review_news_candidates_reuses_news_filter_logic() -> None:
    executor = _FakeExecutor(
        responses=[
            """
            {"items":[
              {"symbol":"600001","name":"Alpha Corp","allowed":true,"risk_notes":[]}
            ],"partial_failures":[]}
            """
        ]
    )
    orchestrator = StockSelectionSubagentOrchestrator(executor=executor, screening_only=False)

    reviewed_items, failures = await orchestrator.review_news_candidates(
        [
            {
                "symbol": "600001",
                "name": "Alpha Corp",
                "candidate_articles": [],
            }
        ]
    )

    assert reviewed_items == [
        {
            "symbol": "600001",
            "name": "Alpha Corp",
            "allowed": True,
            "risk_notes": [],
        }
    ]
    assert failures == []
    assert [label for label, _task, _system, _builtin, _mcp, _light in executor.calls] == ["news-filter"]


@pytest.mark.asyncio
async def test_orchestrator_durable_run_reuses_prepared_input_artifacts() -> None:
    executor = _FakeExecutor(
        responses=[
            """
            {"items":[
              {"symbol":"600001","name":"Alpha Corp","allowed":true,"risk_notes":[]}
            ],"partial_failures":[]}
            """,
            """
            {"symbol":"600001","technical_score":91,
             "score_reasons":["trend is above the short and medium moving averages"],
             "risk_notes":[]}
            """,
        ]
    )
    news_data = _FakeNewsAdapter(
        {
            "600001": AssertionError("news data should not be called"),
        }
    )
    technical_data = _FakeTechnicalAdapter(
        {
            "600001": AssertionError("technical data should not be called"),
        }
    )
    orchestrator = StockSelectionSubagentOrchestrator(
        executor=executor,
        screening_only=False,
        news_data=news_data,
        technical_data=technical_data,
    )
    artifacts: dict[str, Any] = {
        "stock-screening": {
            "screened_items": [{"symbol": "600001", "name": "Alpha Corp"}],
        },
        "prepare-news-filter-inputs": {
            "review_items": [{"symbol": "600001", "name": "Alpha Corp", "candidate_articles": []}],
            "auto_allowed_items": [],
        },
        "prepare-market-scoring-inputs": {
            "scoring_items": [
                {
                    "symbol": "600001",
                    "technical_snapshot": {
                        "symbol": "600001",
                        "close": 12.36,
                        "data_status": "ok",
                    },
                }
            ],
        },
    }
    checkpoints: list[str] = []

    async def checkpoint(stage: str, artifact: dict[str, Any]) -> None:
        del artifact
        checkpoints.append(stage)

    result = await orchestrator.run_stock_report_durable(
        strategy_name="B1",
        trade_date=date(2026, 7, 1),
        artifacts=artifacts,
        checkpoint=checkpoint,
    )

    assert news_data.calls == []
    assert technical_data.calls == []
    assert "finalize" in result
    assert "rendered_report_text" in result["finalize"]
    assert [label for label, *_rest in executor.calls] == ["news-filter", "market-scoring"]
    assert checkpoints == ["news-filter", "market-scoring", "merge", "finalize"]


@pytest.mark.asyncio
async def test_orchestrator_recovery_rebuilds_missing_prepare_artifacts_with_trade_date() -> None:
    executor = _FakeExecutor(
        responses=[
            """
            {"items":[
              {"symbol":"600001","name":"Alpha Corp","allowed":true,"risk_notes":[]}
            ],"partial_failures":[]}
            """,
            """
            {"symbol":"600001","technical_score":91,
             "score_reasons":["trend is above the short and medium moving averages"],
             "risk_notes":[]}
            """,
        ]
    )
    news_data = _FakeNewsAdapter(
        {
            "600001": [NewsArticle(title="historical news", published_at="2026-07-01")],
        }
    )
    technical_data = _FakeTechnicalAdapter(
        {
            "600001": {
                "symbol": "600001",
                "close": 12.36,
                "data_status": "ok",
            }
        }
    )
    orchestrator = StockSelectionSubagentOrchestrator(
        executor=executor,
        screening_only=False,
        news_data=news_data,
        technical_data=technical_data,
    )

    result = await orchestrator.run_stock_report_durable(
        strategy_name="B1",
        trade_date=date(2026, 7, 1),
        artifacts={
            "stock-screening": {
                "screened_items": [{"symbol": "600001", "name": "Alpha Corp"}],
            },
        },
        recovery=True,
    )

    assert result["prepare-news-filter-inputs"] == {
        "review_items": [
            {
                "symbol": "600001",
                "name": "Alpha Corp",
                "candidate_articles": [
                    {
                        "title": "historical news",
                        "summary": "",
                        "date": "2026-07-01",
                        "source": "unknown",
                    }
                ],
            },
        ],
        "auto_allowed_items": [],
    }
    assert news_data.calls == [("600001", 7, date(2026, 7, 1), "Alpha Corp")]
    assert technical_data.calls == [(["600001"], 60, date(2026, 7, 1))]
