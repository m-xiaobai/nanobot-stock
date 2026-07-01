from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

try:
    from nanobot.channels import feishu

    FEISHU_AVAILABLE = getattr(feishu, "FEISHU_AVAILABLE", False)
except ImportError:
    FEISHU_AVAILABLE = False

if not FEISHU_AVAILABLE:
    pytest.skip("Feishu dependencies not installed (lark-oapi)", allow_module_level=True)

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.agent.approval import ApprovalCoordinator, PendingApproval
from nanobot.channels.feishu import FeishuChannel, FeishuConfig


def _make_feishu_channel() -> FeishuChannel:
    channel = FeishuChannel(
        FeishuConfig(
            enabled=True,
            app_id="cli_test",
            app_secret="secret",
            allow_from=["*"],
            topic_isolation=True,
        ),
        MessageBus(),
    )
    channel._client = MagicMock()
    channel._loop = None
    return channel


@pytest.mark.asyncio
async def test_send_approval_uses_interactive_card_with_button_payloads() -> None:
    channel = _make_feishu_channel()
    sends: list[tuple[str, str, str]] = []
    channel._send_message_sync = lambda rid_type, chat_id, msg_type, content: sends.append((rid_type, chat_id, msg_type, content))  # type: ignore[assignment]

    msg = OutboundMessage(
        channel="feishu",
        chat_id="oc_abc",
        content="confirm this change",
        metadata={
            "_mcp_approval": {
                "approvalId": "approval-1",
                "serverName": "mcp-stock-server",
                "toolName": "upsert_stock_daily_bars",
                "sessionKey": "feishu:oc_abc",
            }
        },
    )

    await channel.send(msg)

    assert sends
    _, _, msg_type, content = sends[0]
    assert msg_type == "interactive"
    card = json.loads(content)
    actions = card["elements"][1]["actions"]
    assert actions[0]["value"]["approval_id"] == "approval-1"
    assert actions[0]["value"]["action"] == "approve"
    assert actions[1]["value"]["action"] == "decline"


@pytest.mark.asyncio
async def test_card_action_publishes_structured_approval_reply() -> None:
    channel = _make_feishu_channel()
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value={
                    "kind": "mcp_approval",
                    "approval_id": "approval-1",
                    "action": "approve",
                    "session_key": "feishu:oc_abc:root-1",
                }
            ),
            operator=SimpleNamespace(open_id="ou_user"),
            context=SimpleNamespace(open_chat_id="oc_abc", open_message_id="om_1"),
        )
    )

    await channel._on_card_action(data)

    inbound = await asyncio.wait_for(channel.bus.consume_inbound(), timeout=1.0)
    assert inbound.content == "approve"
    assert inbound.session_key == "feishu:oc_abc:root-1"
    assert inbound.metadata["_mcp_approval"]["approvalId"] == "approval-1"
    assert inbound.metadata["_mcp_approval"]["action"] == "approve"


def test_card_action_sync_returns_result_card_without_buttons() -> None:
    channel = _make_feishu_channel()
    coordinator = ApprovalCoordinator()
    loop = asyncio.new_event_loop()
    try:
        coordinator.register(
            PendingApproval(
                approval_id="approval-1",
                session_key="feishu:oc_abc:root-1",
                server_name="mcp-stock-server",
                tool_name="upsert_stock_daily_bars",
                arguments_summary='{"time":"2026-06-02"}',
                request_context=None,
                future=loop.create_future(),
            )
        )
        setattr(channel.bus, "_agent_loop_approvals", coordinator)
        data = SimpleNamespace(
            event=SimpleNamespace(
                action=SimpleNamespace(
                    value={
                        "kind": "mcp_approval",
                        "approval_id": "approval-1",
                        "action": "approve",
                        "session_key": "feishu:oc_abc:root-1",
                    }
                ),
                operator=SimpleNamespace(open_id="ou_user"),
                context=SimpleNamespace(open_chat_id="oc_abc", open_message_id="om_1"),
            )
        )

        response = channel._on_card_action_sync(data)

        assert response.toast.content == "已批准"
        assert response.card.data["elements"][1]["tag"] == "note"
        assert "已批准" in response.card.data["elements"][1]["elements"][0]["content"]
        assert all(element.get("tag") != "action" for element in response.card.data["elements"])
    finally:
        loop.close()
