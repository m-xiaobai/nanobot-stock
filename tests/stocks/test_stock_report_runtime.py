from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.stocks.runtime import DateSource, StockReportRunStatus, StockReportRuntime


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, date, dict[str, Any]]] = []
        self.recoveries: list[bool] = []

    async def run_stock_report_durable(
        self,
        *,
        strategy_name: str,
        trade_date: date,
        artifacts: dict[str, Any],
        checkpoint=None,
        recovery: bool = False,
    ) -> dict[str, Any]:
        self.recoveries.append(recovery)
        self.calls.append((strategy_name, trade_date, deepcopy(artifacts)))
        artifacts.setdefault("finalize", {})["rendered_report_text"] = (
            f"report {strategy_name} {trade_date.isoformat()}"
        )
        return artifacts


class _CheckpointThenFailRunner:
    async def run_stock_report_durable(
        self,
        *,
        strategy_name: str,
        trade_date: date,
        artifacts: dict[str, Any],
        checkpoint=None,
        recovery: bool = False,
    ) -> dict[str, Any]:
        del recovery
        artifacts["prepare-news-filter-inputs"] = {"review_items": [], "auto_allowed_items": []}
        await checkpoint("prepare-news-filter-inputs", artifacts["prepare-news-filter-inputs"])
        raise RuntimeError("boom")


class _CollectingBus:
    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        self.messages.append(msg)


@pytest.mark.asyncio
async def test_runtime_persists_explicit_date_run_and_delivers_report(tmp_path: Path) -> None:
    runner = _FakeRunner()
    bus = _CollectingBus()
    runtime = StockReportRuntime(workspace=tmp_path, runner=runner, bus=bus)

    run = runtime.enqueue(
        strategy_name="B1",
        trade_date=date(2026, 7, 1),
        date_source=DateSource.EXPLICIT,
        channel="cli",
        chat_id="direct",
        session_key="cli:direct",
    )
    await runtime.run_once(run.run_id)

    saved = runtime.store.load(run.run_id)
    assert saved.status == StockReportRunStatus.COMPLETED
    assert saved.delivery_status == "sent"
    assert saved.date_source == DateSource.EXPLICIT
    assert runner.calls == [("B1", date(2026, 7, 1), {})]
    assert runner.recoveries == [False]
    assert bus.messages[0].content == "report B1 2026-07-01"


@pytest.mark.asyncio
async def test_runtime_marks_implicit_cross_day_run_stale(tmp_path: Path) -> None:
    runner = _FakeRunner()
    runtime = StockReportRuntime(
        workspace=tmp_path,
        runner=runner,
        bus=_CollectingBus(),
        today=lambda: date(2026, 7, 2),
    )
    run = runtime.enqueue(
        strategy_name="B1",
        trade_date=date(2026, 7, 1),
        date_source=DateSource.IMPLICIT_DEFAULT,
        channel="cli",
        chat_id="direct",
        session_key="cli:direct",
    )

    await runtime.resume_incomplete_runs()

    saved = runtime.store.load(run.run_id)
    assert saved.status == StockReportRunStatus.STALE
    assert runner.calls == []


@pytest.mark.asyncio
async def test_runtime_marks_interrupted_running_run_as_recovery(tmp_path: Path) -> None:
    runner = _FakeRunner()
    runtime = StockReportRuntime(workspace=tmp_path, runner=runner, bus=_CollectingBus())
    run = runtime.enqueue(
        strategy_name="B1",
        trade_date=date(2026, 7, 1),
        date_source=DateSource.EXPLICIT,
        channel="cli",
        chat_id="direct",
        session_key="cli:direct",
    )
    run.status = StockReportRunStatus.RUNNING
    runtime.store.save(run)

    await runtime.run_once(run.run_id)

    assert runner.recoveries == [True]


@pytest.mark.asyncio
async def test_runtime_redelivers_completed_pending_report_without_recomputing(tmp_path: Path) -> None:
    runner = _FakeRunner()
    bus = _CollectingBus()
    runtime = StockReportRuntime(workspace=tmp_path, runner=runner, bus=bus)
    run = runtime.enqueue(
        strategy_name="B1",
        trade_date=date(2026, 7, 1),
        date_source=DateSource.EXPLICIT,
        channel="cli",
        chat_id="direct",
        session_key="cli:direct",
    )
    run.status = StockReportRunStatus.COMPLETED
    run.delivery_status = "pending"
    run.artifacts["finalize"] = {"rendered_report_text": "cached report"}
    runtime.store.save(run)

    await runtime.resume_incomplete_runs()

    saved = runtime.store.load(run.run_id)
    assert saved.delivery_status == "sent"
    assert runner.calls == []
    assert [msg.content for msg in bus.messages] == ["cached report"]


@pytest.mark.asyncio
async def test_run_once_redelivers_completed_pending_report_without_recomputing(
    tmp_path: Path,
) -> None:
    runner = _FakeRunner()
    bus = _CollectingBus()
    runtime = StockReportRuntime(workspace=tmp_path, runner=runner, bus=bus)
    run = runtime.enqueue(
        strategy_name="B1",
        trade_date=date(2026, 7, 1),
        date_source=DateSource.EXPLICIT,
        channel="cli",
        chat_id="direct",
        session_key="cli:direct",
    )
    run.status = StockReportRunStatus.COMPLETED
    run.delivery_status = "pending"
    run.artifacts["finalize"] = {"rendered_report_text": "cached report"}
    runtime.store.save(run)

    await runtime.run_once(run.run_id)

    saved = runtime.store.load(run.run_id)
    assert saved.delivery_status == "sent"
    assert runner.calls == []
    assert [msg.content for msg in bus.messages] == ["cached report"]


def test_store_cleanup_keeps_pending_delivery_and_removes_expired_terminal_runs(tmp_path: Path) -> None:
    runtime = StockReportRuntime(workspace=tmp_path, runner=_FakeRunner(), bus=_CollectingBus())
    old = datetime.now() - timedelta(days=4)

    sent = runtime.enqueue(
        strategy_name="B1",
        trade_date=date(2026, 7, 1),
        date_source=DateSource.EXPLICIT,
        channel="cli",
        chat_id="sent",
        session_key="cli:sent",
    )
    sent.status = StockReportRunStatus.COMPLETED
    sent.delivery_status = "sent"
    sent.updated_at = old
    runtime.store.save(sent, touch=False)

    pending = runtime.enqueue(
        strategy_name="B1",
        trade_date=date(2026, 7, 1),
        date_source=DateSource.EXPLICIT,
        channel="cli",
        chat_id="pending",
        session_key="cli:pending",
    )
    pending.status = StockReportRunStatus.COMPLETED
    pending.delivery_status = "pending"
    pending.updated_at = old
    runtime.store.save(pending, touch=False)

    runtime.cleanup_finished_runs(now=datetime.now())

    assert runtime.store.load(sent.run_id) is None
    assert runtime.store.load(pending.run_id) is not None


@pytest.mark.asyncio
async def test_runtime_persists_stage_checkpoint_before_failed_run(tmp_path: Path) -> None:
    runtime = StockReportRuntime(
        workspace=tmp_path,
        runner=_CheckpointThenFailRunner(),
        bus=_CollectingBus(),
    )
    run = runtime.enqueue(
        strategy_name="B1",
        trade_date=date(2026, 7, 1),
        date_source=DateSource.EXPLICIT,
        channel="cli",
        chat_id="direct",
        session_key="cli:direct",
    )

    await runtime.run_once(run.run_id)

    saved = runtime.store.load(run.run_id)
    assert saved.status == StockReportRunStatus.FAILED
    assert saved.stage_statuses["prepare-news-filter-inputs"] == "completed"
    assert saved.artifacts["prepare-news-filter-inputs"] == {
        "review_items": [],
        "auto_allowed_items": [],
    }
