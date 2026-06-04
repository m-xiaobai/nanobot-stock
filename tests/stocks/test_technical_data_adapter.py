from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from nanobot.stocks.technical_data_adapter import MCPTechnicalDataAdapter


class _FakeTool:
    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        return self._response


class _FakeRegistry:
    def __init__(self, tool: _FakeTool) -> None:
        self._tool = tool

    def get(self, name: str) -> _FakeTool | None:
        if name != "mcp_stocks_get_technical_snapshot":
            return None
        return self._tool


def test_adapter_maps_batch_request_to_mcp_tool() -> None:
    tool = _FakeTool(
        '{"items":[{"symbol":"600001","close":12.3},{"symbol":"000001","close":9.1}]}'
    )
    adapter = MCPTechnicalDataAdapter(server_name="stocks", tool_registry=_FakeRegistry(tool))

    result = adapter.get_technical_snapshot(["600001", "000001"], 60, date(2026, 5, 26))

    assert tool.calls == [
        {
            "symbols": ["600001", "000001"],
            "trade_date": "2026-05-26",
            "lookback_days": 60,
            "include_bars": False,
        }
    ]
    assert result == {
        "600001": {
            "symbol": "600001",
            "close": 12.3,
            "lookback_days": 60,
            "data_status": "ok",
        },
        "000001": {
            "symbol": "000001",
            "close": 9.1,
            "lookback_days": 60,
            "data_status": "ok",
        },
    }


def test_adapter_rejects_empty_symbol_batches() -> None:
    tool = _FakeTool('{"items":[{"symbol":"600001","close":12.3}]}')
    adapter = MCPTechnicalDataAdapter(server_name="stocks", tool_registry=_FakeRegistry(tool))

    with pytest.raises(ValueError, match="at least one symbol"):
        adapter.get_technical_snapshot([], 60, date(2026, 5, 26))

    assert tool.calls == []
