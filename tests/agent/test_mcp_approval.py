from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.approval import ApprovalCoordinator, PendingApproval
from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus


def _make_loop(tmp_path: Path) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    with patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager") as mock_subagent, \
         patch("nanobot.agent.loop.Dream"):
        mock_subagent.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=tmp_path,
        )
    return loop


@pytest.mark.asyncio
async def test_approval_coordinator_rejects_second_pending_in_same_session() -> None:
    coordinator = ApprovalCoordinator()
    loop = asyncio.get_running_loop()
    coordinator.register(
        PendingApproval(
            approval_id="approval-1",
            session_key="feishu:chat1",
            server_name="srv",
            tool_name="tool_a",
            arguments_summary="{}",
            request_context=None,
            future=loop.create_future(),
        )
    )

    with pytest.raises(ValueError):
        coordinator.register(
            PendingApproval(
                approval_id="approval-2",
                session_key="feishu:chat1",
                server_name="srv",
                tool_name="tool_b",
                arguments_summary="{}",
                request_context=None,
                future=loop.create_future(),
            )
        )


@pytest.mark.asyncio
async def test_agent_loop_consumes_pending_approval_before_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _make_loop(tmp_path)
    loop._connect_mcp = AsyncMock(return_value=None)  # type: ignore[method-assign]
    loop._dispatch = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr("nanobot.agent.loop.agent_context.handle_runtime_control", AsyncMock(return_value=False))

    future: asyncio.Future[InboundMessage] = asyncio.get_running_loop().create_future()
    loop.approvals.register(
        PendingApproval(
            approval_id="approval-1",
            session_key="feishu:chat1",
            server_name="srv",
            tool_name="tool_a",
            arguments_summary="{}",
            request_context=SimpleNamespace(),
            future=future,
        )
    )

    task = asyncio.create_task(loop.run())
    try:
        await loop.bus.publish_inbound(
            InboundMessage(
                channel="feishu",
                sender_id="user1",
                chat_id="chat1",
                content="/approve approval-1",
            )
        )
        inbound = await asyncio.wait_for(future, timeout=1.0)
        assert inbound.content == "/approve approval-1"
        loop._dispatch.assert_not_called()
    finally:
        loop.stop()
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_agent_loop_does_not_consume_plain_text_when_approval_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _make_loop(tmp_path)
    loop._connect_mcp = AsyncMock(return_value=None)  # type: ignore[method-assign]
    loop._dispatch = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr("nanobot.agent.loop.agent_context.handle_runtime_control", AsyncMock(return_value=False))

    future: asyncio.Future[InboundMessage] = asyncio.get_running_loop().create_future()
    loop.approvals.register(
        PendingApproval(
            approval_id="approval-1",
            session_key="feishu:chat1",
            server_name="srv",
            tool_name="tool_a",
            arguments_summary="{}",
            request_context=SimpleNamespace(),
            future=future,
        )
    )

    task = asyncio.create_task(loop.run())
    try:
        await loop.bus.publish_inbound(
            InboundMessage(
                channel="feishu",
                sender_id="user1",
                chat_id="chat1",
                content="hello there",
            )
        )
        await asyncio.sleep(0.05)
        assert future.done() is False
        loop._dispatch.assert_called_once()
    finally:
        loop.stop()
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
