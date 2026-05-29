from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.command.builtin import build_help_text, builtin_command_palette, cmd_stock_report, register_builtin_commands
from nanobot.command.router import CommandContext, CommandRouter
from nanobot.stocks.service import DailySelectionReport, SelectedStockReport


def _ctx(raw: str, args: str = "", *, service=None) -> CommandContext:
    msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content=raw)
    loop = SimpleNamespace(stock_selection_service=service)
    return CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, args=args, loop=loop)


class _FakeService:
    def __init__(self, report: DailySelectionReport) -> None:
        self.report = report
        self.calls: list[tuple[str, date]] = []

    def run_daily_stock_selection(self, strategy_name: str, trade_date: date) -> DailySelectionReport:
        self.calls.append((strategy_name, trade_date))
        return self.report


class _AsyncFakeService:
    def __init__(self, report: DailySelectionReport) -> None:
        self.report = report
        self.calls: list[tuple[str, date]] = []

    async def run_daily_stock_selection(self, strategy_name: str, trade_date: date) -> DailySelectionReport:
        self.calls.append((strategy_name, trade_date))
        return self.report


@pytest.mark.asyncio
async def test_stock_report_command_shows_usage_without_args() -> None:
    out = await cmd_stock_report(_ctx("/stock-report"))

    assert "Usage: /stock-report" in out.content


@pytest.mark.asyncio
async def test_stock_report_command_requires_service_configuration() -> None:
    out = await cmd_stock_report(_ctx("/stock-report breakout_volume", args="breakout_volume"))

    assert "stock selection service is not configured" in out.content


@pytest.mark.asyncio
async def test_stock_report_command_runs_service_and_formats_report() -> None:
    report = DailySelectionReport(
        trade_date=date(2026, 5, 26),
        strategy_name="breakout_volume",
        market="A",
        selected_stocks=[
            SelectedStockReport(
                symbol="600001",
                strategy_name="breakout_volume",
                screen_pass_reasons=["close broke above the recent range high"],
                negative_news_flags=[],
                technical_score=95,
                score_reasons=["volume confirms the move"],
                risk_notes=["watch for next-day follow-through"],
                report_date=date(2026, 5, 26),
            )
        ],
        summary="1 candidate selected.",
        global_risk_disclaimer="For research use only.",
        partial_failures=[],
    )
    service = _FakeService(report)

    out = await cmd_stock_report(
        _ctx("/stock-report breakout_volume 2026-05-26", args="breakout_volume 2026-05-26", service=service)
    )

    assert service.calls == [("breakout_volume", date(2026, 5, 26))]
    assert "## Stock Report" in out.content
    assert "- Strategy: `breakout_volume`" in out.content
    assert "- Trade date: `2026-05-26`" in out.content
    assert "- `600001` score `95`" in out.content
    assert "close broke above the recent range high" in out.content
    assert "For research use only." in out.content
    assert out.metadata["render_as"] == "text"


@pytest.mark.asyncio
async def test_stock_report_command_prefers_async_orchestrator_when_present() -> None:
    report = DailySelectionReport(
        trade_date=date(2026, 5, 26),
        strategy_name="breakout_volume",
        market="A",
        selected_stocks=[],
        summary="async report",
        global_risk_disclaimer="For research use only.",
        partial_failures=[],
    )
    async_service = _AsyncFakeService(report)
    msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/stock-report breakout_volume")
    loop = SimpleNamespace(stock_selection_service=None, stock_selection_orchestrator=async_service)
    ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw=msg.content, args="breakout_volume", loop=loop)

    out = await cmd_stock_report(ctx)

    assert async_service.calls == [("breakout_volume", date.today())]
    assert "async report" in out.content


@pytest.mark.asyncio
async def test_stock_report_command_is_registered_on_router() -> None:
    router = CommandRouter()
    register_builtin_commands(router)
    report = DailySelectionReport(
        trade_date=date(2026, 5, 26),
        strategy_name="breakout_volume",
        market="A",
        selected_stocks=[],
        summary="no candidates",
        global_risk_disclaimer="For research use only.",
        partial_failures=[],
    )
    service = _FakeService(report)

    out = await router.dispatch(
        _ctx("/stock-report breakout_volume", args="breakout_volume", service=service)
    )

    assert out is not None
    assert "## Stock Report" in out.content


def test_stock_report_command_in_help_and_palette() -> None:
    palette = builtin_command_palette()

    assert any(item["command"] == "/stock-report" and item["arg_hint"] == "<strategy> [YYYY-MM-DD]" for item in palette)
    assert "/stock-report <strategy> [YYYY-MM-DD]" in build_help_text()
