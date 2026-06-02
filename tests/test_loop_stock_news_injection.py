from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

from nanobot.bus.queue import MessageBus


def test_agent_loop_injects_real_stock_news_adapter(tmp_path) -> None:
    fake_loguru = types.ModuleType("loguru")
    fake_loguru.logger = MagicMock()
    sys.modules.setdefault("loguru", fake_loguru)

    from nanobot.agent.loop import AgentLoop

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4096

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )

    assert loop.stock_selection_orchestrator.news_data is not None
    assert (
        loop.stock_selection_orchestrator.news_data.__class__.__name__
        == "EastmoneySinaNewsAdapter"
    )
