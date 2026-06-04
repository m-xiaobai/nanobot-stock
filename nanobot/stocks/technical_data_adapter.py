"""Technical data adapter for market-scoring inputs."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanobot.agent.tools.base import Tool
    from nanobot.agent.tools.registry import ToolRegistry


class MCPTechnicalDataAdapter:
    """Fetch technical snapshots directly from a configured MCP server."""

    def __init__(
        self,
        *,
        server_name: str,
        tool_name: str = "get_technical_snapshot",
        lookback_days: int = 60,
        tool_registry: "ToolRegistry | None" = None,
    ) -> None:
        self._server_name = server_name
        self._tool_name = tool_name
        self._lookback_days = lookback_days
        self._tool_registry = tool_registry

    def get_technical_snapshot(
        self,
        symbols: list[str],
        lookback_days: int,
        anchor_date: date | None = None,
    ) -> dict[str, dict[str, Any]]:
        import asyncio

        shared_tool = self._get_registered_tool()
        if shared_tool is None:
            tool_name = f"mcp_{self._server_name}_{self._tool_name}"
            raise RuntimeError(
                f"MCP tool '{tool_name}' is not registered in the shared tool registry"
            )

        symbols = list(symbols)
        if not symbols:
            raise ValueError("at least one symbol is required for technical snapshot retrieval")

        async def _run_shared() -> dict[str, dict[str, Any]]:
            params = {
                "symbols": symbols,
                "trade_date": anchor_date.isoformat() if anchor_date is not None else None,
                "lookback_days": lookback_days or self._lookback_days,
                "include_bars": False,
            }
            raw = await shared_tool.execute(**params)
            return self._parse_snapshot_payload(raw, symbols, lookback_days)

        return asyncio.run(_run_shared())

    def _get_registered_tool(self) -> "Tool | None":
        if self._tool_registry is None:
            return None
        tool_name = f"mcp_{self._server_name}_{self._tool_name}"
        return self._tool_registry.get(tool_name)

    @staticmethod
    def _parse_snapshot_payload(
        raw: Any,
        symbols: list[str],
        lookback_days: int,
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, str):
            raise TypeError("MCP technical snapshot payload must be text JSON")
        raw = raw.strip()
        if not raw:
            raise ValueError("MCP technical snapshot payload is empty")
        import json

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"MCP technical snapshot payload returned invalid JSON: {exc}") from exc

        if not isinstance(parsed, dict):
            raise TypeError("MCP technical snapshot payload must be a JSON object")

        items = parsed.get("items")
        if not isinstance(items, list):
            raise TypeError("MCP technical snapshot payload must contain a list 'items'")

        snapshots: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                raise TypeError("MCP technical snapshot items must be JSON objects")
            raw_symbol = item.get("symbol")
            if not isinstance(raw_symbol, str) or not raw_symbol:
                raise TypeError("MCP technical snapshot items must contain non-empty string symbol")
            snapshot = dict(item)
            snapshot.setdefault("symbol", raw_symbol)
            snapshot.setdefault("lookback_days", lookback_days)
            snapshot.setdefault("data_status", "ok")
            snapshots[raw_symbol] = snapshot

        for symbol in symbols:
            if symbol not in snapshots:
                continue
            snapshots[symbol].setdefault("symbol", symbol)

        return snapshots
