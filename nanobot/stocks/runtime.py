"""Durable runtime for `/stock-report` jobs."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from nanobot.bus.events import OutboundMessage


class DateSource(StrEnum):
    EXPLICIT = "explicit"
    IMPLICIT_DEFAULT = "implicit_default"


class StockReportRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_RETRY = "waiting_retry"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"
    CANCELLED = "cancelled"


_TERMINAL_STATUSES = {
    StockReportRunStatus.COMPLETED,
    StockReportRunStatus.FAILED,
    StockReportRunStatus.STALE,
    StockReportRunStatus.CANCELLED,
}


@dataclass
class StockReportRun:
    run_id: str
    session_key: str
    channel: str
    chat_id: str
    strategy_name: str
    trade_date: date
    date_source: DateSource
    requested_at: datetime
    status: StockReportRunStatus = StockReportRunStatus.QUEUED
    current_stage: str | None = None
    stage_statuses: dict[str, str] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    partial_failures: list[str] = field(default_factory=list)
    delivery_status: str = "pending"
    error: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_key": self.session_key,
            "channel": self.channel,
            "chat_id": self.chat_id,
            "strategy_name": self.strategy_name,
            "trade_date": self.trade_date.isoformat(),
            "date_source": self.date_source.value,
            "requested_at": self.requested_at.isoformat(),
            "status": self.status.value,
            "current_stage": self.current_stage,
            "stage_statuses": self.stage_statuses,
            "artifacts": self.artifacts,
            "partial_failures": self.partial_failures,
            "delivery_status": self.delivery_status,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StockReportRun":
        return cls(
            run_id=str(data["run_id"]),
            session_key=str(data["session_key"]),
            channel=str(data["channel"]),
            chat_id=str(data["chat_id"]),
            strategy_name=str(data["strategy_name"]),
            trade_date=date.fromisoformat(str(data["trade_date"])),
            date_source=DateSource(str(data["date_source"])),
            requested_at=datetime.fromisoformat(str(data["requested_at"])),
            status=StockReportRunStatus(str(data.get("status", StockReportRunStatus.QUEUED))),
            current_stage=data.get("current_stage"),
            stage_statuses=dict(data.get("stage_statuses") or {}),
            artifacts=dict(data.get("artifacts") or {}),
            partial_failures=list(data.get("partial_failures") or []),
            delivery_status=str(data.get("delivery_status") or "pending"),
            error=data.get("error"),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            updated_at=datetime.fromisoformat(str(data["updated_at"])),
        )


class StockReportRunStore:
    def __init__(self, workspace: Path) -> None:
        self.root = workspace / "stock-report"
        self.runs_dir = self.root / "runs"
        self.index_path = self.root / "index.jsonl"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def save(self, run: StockReportRun, *, touch: bool = True) -> None:
        if touch:
            run.updated_at = datetime.now()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(run.run_id)
        tmp_path = path.with_suffix(".json.tmp")
        content = json.dumps(run.to_dict(), ensure_ascii=False, indent=2)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def append_index(self, run: StockReportRun) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.index_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(run.to_dict(), ensure_ascii=False) + "\n")

    def load(self, run_id: str) -> StockReportRun | None:
        path = self.path_for(run_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return StockReportRun.from_dict(data)

    def list_runs(self) -> list[StockReportRun]:
        runs: list[StockReportRun] = []
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                runs.append(StockReportRun.from_dict(data))
            except Exception:
                continue
        return runs

    def delete(self, run_id: str) -> None:
        self.path_for(run_id).unlink(missing_ok=True)


class StockReportRuntime:
    def __init__(
        self,
        *,
        workspace: Path,
        runner: Any,
        bus: Any,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.store = StockReportRunStore(workspace)
        self.runner = runner
        self.bus = bus
        self._today = today or date.today
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self.cleanup_finished_runs()
        self.schedule_resume()

    def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    def schedule_resume(self) -> None:
        self._schedule(self.resume_incomplete_runs())

    def schedule_run(self, run_id: str) -> None:
        self._schedule(self.run_once(run_id))

    def _schedule(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def enqueue(
        self,
        *,
        strategy_name: str,
        trade_date: date,
        date_source: DateSource,
        channel: str,
        chat_id: str,
        session_key: str,
    ) -> StockReportRun:
        run = StockReportRun(
            run_id=uuid.uuid4().hex[:12],
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            strategy_name=strategy_name,
            trade_date=trade_date,
            date_source=date_source,
            requested_at=datetime.now(),
        )
        self.store.save(run)
        self.store.append_index(run)
        return run

    async def run_once(self, run_id: str) -> None:
        run = self.store.load(run_id)
        if run is None:
            return
        if run.status == StockReportRunStatus.COMPLETED:
            if run.delivery_status != "sent":
                await self._deliver_completed(run)
            return
        recovering = run.status in {
            StockReportRunStatus.RUNNING,
            StockReportRunStatus.WAITING_RETRY,
        }
        if not self._can_resume(run):
            return
        run.status = StockReportRunStatus.RUNNING
        self.store.save(run)

        async def checkpoint(stage: str, artifact: Any) -> None:
            run.current_stage = stage
            run.stage_statuses[stage] = "completed"
            run.artifacts[stage] = artifact
            self.store.save(run)

        try:
            artifacts = await self.runner.run_stock_report_durable(
                strategy_name=run.strategy_name,
                trade_date=run.trade_date,
                artifacts=run.artifacts,
                checkpoint=checkpoint,
                recovery=recovering,
            )
            run.artifacts = artifacts
            run.status = StockReportRunStatus.COMPLETED
            self.store.save(run)
            await self._deliver_completed(run)
        except Exception as exc:
            run.status = StockReportRunStatus.FAILED
            run.error = str(exc)
            self.store.save(run)

    async def resume_incomplete_runs(self) -> None:
        for run in self.store.list_runs():
            if run.status == StockReportRunStatus.COMPLETED and run.delivery_status != "sent":
                await self._deliver_completed(run)
                continue
            if run.status in {
                StockReportRunStatus.QUEUED,
                StockReportRunStatus.RUNNING,
                StockReportRunStatus.WAITING_RETRY,
            }:
                await self.run_once(run.run_id)

    def cleanup_finished_runs(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now()
        ttl_by_status = {
            StockReportRunStatus.COMPLETED: timedelta(days=3),
            StockReportRunStatus.FAILED: timedelta(days=3),
            StockReportRunStatus.STALE: timedelta(days=2),
            StockReportRunStatus.CANCELLED: timedelta(days=2),
        }
        for run in self.store.list_runs():
            if run.status not in _TERMINAL_STATUSES:
                continue
            if run.status == StockReportRunStatus.COMPLETED and run.delivery_status != "sent":
                continue
            ttl = ttl_by_status.get(run.status)
            if ttl is not None and now - run.updated_at > ttl:
                self.store.delete(run.run_id)

    def _can_resume(self, run: StockReportRun) -> bool:
        if run.date_source == DateSource.EXPLICIT:
            return True
        if run.trade_date == self._today():
            return True
        run.status = StockReportRunStatus.STALE
        run.error = (
            "implicit default date run crossed into a new day; "
            "manual restart is required"
        )
        self.store.save(run)
        return False

    async def _deliver_completed(self, run: StockReportRun) -> None:
        text = str(run.artifacts.get("finalize", {}).get("rendered_report_text") or "").strip()
        if not text:
            run.status = StockReportRunStatus.FAILED
            run.error = "completed run has no rendered report text"
            self.store.save(run)
            return
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=run.channel,
                chat_id=run.chat_id,
                content=text,
                metadata={"render_as": "text", "stock_report_run_id": run.run_id},
            )
        )
        run.delivery_status = "sent"
        self.store.save(run)
