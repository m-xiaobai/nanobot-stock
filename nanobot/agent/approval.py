"""Host-side approval coordination for sensitive MCP tool calls."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from nanobot.bus.events import InboundMessage

_APPROVE_CMD = re.compile(r"^/approve\s+([A-Za-z0-9_-]+)\s*$", re.IGNORECASE)
_DECLINE_CMD = re.compile(r"^/decline\s+([A-Za-z0-9_-]+)\s*$", re.IGNORECASE)


@dataclass
class PendingApproval:
    approval_id: str
    session_key: str
    server_name: str
    tool_name: str
    arguments_summary: str
    request_context: Any
    future: asyncio.Future[InboundMessage]
    created_at: float = field(default_factory=time.monotonic)
    timeout_s: float = 300.0


class ApprovalCoordinator:
    """Track one active approval per session and route replies back to it."""

    def __init__(self) -> None:
        self._pending_by_id: dict[str, PendingApproval] = {}
        self._pending_by_session: dict[str, str] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending_by_id)

    def register(self, request: PendingApproval) -> None:
        self.expire()
        existing = self._pending_by_session.get(request.session_key)
        if existing is not None:
            raise ValueError(f"approval already pending in session {request.session_key}")
        self._pending_by_id[request.approval_id] = request
        self._pending_by_session[request.session_key] = request.approval_id

    @staticmethod
    def _approval_id_from_message(msg: InboundMessage) -> str | None:
        metadata = msg.metadata or {}
        approval_meta = metadata.get("_mcp_approval")
        if isinstance(approval_meta, dict):
            approval_id = approval_meta.get("approvalId")
            if isinstance(approval_id, str) and approval_id.strip():
                return approval_id.strip()

        content = (msg.content or "").strip()
        match = _APPROVE_CMD.match(content) or _DECLINE_CMD.match(content)
        if match:
            return match.group(1)
        return None

    def match(self, msg: InboundMessage) -> PendingApproval | None:
        self.expire()
        approval_id = self._approval_id_from_message(msg)
        if approval_id is None:
            return None
        request = self._pending_by_id.get(approval_id)
        if request is None or request.session_key != msg.session_key:
            return None
        return request

    def consume(self, msg: InboundMessage) -> bool:
        request = self.match(msg)
        if request is None:
            return False
        self.cancel(request.approval_id, result=msg)
        return True

    def cancel(self, approval_id: str, *, result: InboundMessage | None = None) -> None:
        request = self._pending_by_id.pop(approval_id, None)
        if request is None:
            return
        self._pending_by_session.pop(request.session_key, None)
        if request.future.done():
            return
        if result is None:
            request.future.cancel()
        else:
            request.future.set_result(result)

    def expire(self) -> None:
        now = time.monotonic()
        expired = [
            approval_id
            for approval_id, request in self._pending_by_id.items()
            if now - request.created_at >= request.timeout_s
        ]
        for approval_id in expired:
            self.cancel(approval_id)
