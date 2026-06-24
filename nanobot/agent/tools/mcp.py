"""MCP client: connects to MCP servers and wraps their tools as native nanobot tools."""

import asyncio
import json
import os
import re
import shutil
import urllib.parse
from contextlib import AsyncExitStack, suppress
from contextvars import ContextVar
from typing import Any, Callable, Mapping
from weakref import WeakKeyDictionary

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import (
    INBOUND_META_RUNTIME_CONTROL,
    RUNTIME_CONTROL_ACK,
    RUNTIME_CONTROL_MCP_RELOAD,
    InboundMessage,
    OutboundMessage,
)

# Transient connection errors that warrant a single retry.
# These typically happen when an MCP server restarts or a network
# connection is interrupted between calls.
_TRANSIENT_EXC_NAMES: frozenset[str] = frozenset((
    "ClosedResourceError",
    "BrokenResourceError",
    "EndOfStream",
    "BrokenPipeError",
    "ConnectionResetError",
    "ConnectionRefusedError",
    "ConnectionAbortedError",
    "ConnectionError",
))

_WINDOWS_SHELL_LAUNCHERS: frozenset[str] = frozenset(("npx", "npm", "pnpm", "yarn", "bunx"))
_TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
_CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
_TASK_SUPPORT_OPTIONAL = "optional"
_TASK_SUPPORT_REQUIRED = "required"
_TASK_SUPPORT_VALUES = frozenset((_TASK_SUPPORT_OPTIONAL, _TASK_SUPPORT_REQUIRED))
_TASK_BACKEND_NONE = "plain"
_TASK_BACKEND_STANDARD = "server_directed_task"
_TASK_BACKEND_PROACTIVE = "proactive_task"
_TASK_DEFAULT_TTL_MS = 60000
_ELICITATION_TIMEOUT_SECONDS = 300.0
_ACTIVE_REQUEST_CONTEXT_ATTR = "_nanobot_active_request_context"

_CURRENT_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "mcp_current_request_context",
    default=None,
)

# Characters allowed in tool names by model providers (Anthropic, OpenAI, etc.).
# Replace anything outside [a-zA-Z0-9_-] with underscore and collapse runs.
_SANITIZE_RE = re.compile(r"_+")
_RELOAD_LOCKS: WeakKeyDictionary[Any, asyncio.Lock] = WeakKeyDictionary()


def _sanitize_name(name: str) -> str:
    """Sanitize an MCP-derived name for model API compatibility."""
    return _SANITIZE_RE.sub("_", re.sub(r"[^a-zA-Z0-9_-]", "_", name))


def _is_transient(exc: BaseException) -> bool:
    """Check if an exception looks like a transient connection error."""
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES


async def _probe_http_url(url: str, timeout: float = 3.0) -> bool:
    """Quick TCP probe to check if an HTTP MCP server is reachable.

    Avoids entering ``streamable_http_client`` / ``sse_client`` when the port is
    closed — those transports use anyio task groups whose cleanup can raise
    ``RuntimeError`` / ``ExceptionGroup`` that escape the caller's try/except
    and crash the event loop.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if not port:
        port = 443 if parsed.scheme == "https" else 80
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def _windows_command_basename(command: str) -> str:
    """Return the lowercase basename for a Windows command or path."""
    return command.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()


def _normalize_windows_stdio_command(
    command: str,
    args: list[str] | None,
    env: dict[str, str] | None,
) -> tuple[str, list[str], dict[str, str] | None]:
    """Wrap Windows shell launchers so MCP stdio servers start reliably."""
    normalized_args = list(args or [])
    if os.name != "nt":
        return command, normalized_args, env

    basename = _windows_command_basename(command)
    if basename in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return command, normalized_args, env

    if basename.endswith((".exe", ".com")):
        return command, normalized_args, env

    resolved = shutil.which(command, path=(env or {}).get("PATH")) or command
    resolved_basename = _windows_command_basename(resolved)
    should_wrap = (
        basename in _WINDOWS_SHELL_LAUNCHERS
        or basename.endswith((".cmd", ".bat"))
        or resolved_basename.endswith((".cmd", ".bat"))
    )
    if not should_wrap:
        return command, normalized_args, env

    comspec = (env or {}).get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    return comspec, ["/d", "/c", command, *normalized_args], env


def _extract_nullable_branch(options: Any) -> tuple[dict[str, Any], bool] | None:
    """Return the single non-null branch for nullable unions."""
    if not isinstance(options, list):
        return None

    non_null: list[dict[str, Any]] = []
    saw_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            saw_null = True
            continue
        non_null.append(option)

    if saw_null and len(non_null) == 1:
        return non_null[0], True
    return None


def _normalize_schema_for_openai(schema: Any) -> dict[str, Any]:
    """Normalize only nullable JSON Schema patterns for tool definitions."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    normalized = dict(schema)

    raw_type = normalized.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            normalized["type"] = non_null[0]
            normalized["nullable"] = True

    for key in ("oneOf", "anyOf"):
        nullable_branch = _extract_nullable_branch(normalized.get(key))
        if nullable_branch is not None:
            branch, _ = nullable_branch
            merged = {k: v for k, v in normalized.items() if k != key}
            merged.update(branch)
            normalized = merged
            normalized["nullable"] = True
            break

    if "properties" in normalized and isinstance(normalized["properties"], dict):
        normalized["properties"] = {
            name: _normalize_schema_for_openai(prop) if isinstance(prop, dict) else prop
            for name, prop in normalized["properties"].items()
        }

    if "items" in normalized and isinstance(normalized["items"], dict):
        normalized["items"] = _normalize_schema_for_openai(normalized["items"])

    if normalized.get("type") != "object":
        return normalized

    normalized.setdefault("properties", {})
    normalized.setdefault("required", [])
    return normalized


def _extract_tool_task_support(tool_def: Any) -> str | None:
    """Return the tool's task support mode if present."""
    execution = getattr(tool_def, "execution", None)
    if execution is None:
        return None
    if isinstance(execution, dict):
        task_support = execution.get("taskSupport")
    else:
        task_support = getattr(execution, "taskSupport", None)
    if isinstance(task_support, str) and task_support in _TASK_SUPPORT_VALUES:
        return task_support
    return None


def _supports_task_extension(capabilities: Any) -> bool:
    """Return whether the negotiated server capabilities include Tasks support."""
    if capabilities is None:
        return False

    if getattr(capabilities, "tasks", None) is not None:
        return True

    extensions = getattr(capabilities, "extensions", None)
    if isinstance(extensions, Mapping) and _TASKS_EXTENSION_ID in extensions:
        return True

    experimental = getattr(capabilities, "experimental", None)
    if isinstance(experimental, Mapping) and _TASKS_EXTENSION_ID in experimental:
        return True
    return False


def _client_task_extensions() -> dict[str, dict[str, Any]]:
    """Return the client capability extension block for the Tasks extension."""
    return {"extensions": {_TASKS_EXTENSION_ID: {}}}


def _format_elicitation_prompt(params: Any) -> str:
    """Render a human-readable prompt for a server elicitation request."""
    message = str(getattr(params, "message", "") or "").strip()
    mode = str(getattr(params, "mode", "") or "").strip()

    parts = ["[MCP 确认]"]
    if message:
        parts.append(message)

    if mode == "url":
        url = str(getattr(params, "url", "") or "").strip()
        if url:
            parts.append(f"URL: {url}")
        parts.append("请回复 `confirm` 继续，或回复 `cancel` 取消。")
    else:
        parts.append("请直接回复 JSON 进行确认。")
        parts.append("示例：")
        parts.append(json.dumps({"confirm": True, "reason": "同意执行"}, ensure_ascii=False))
        parts.append("如果要取消，也可以直接回复 `cancel`。")

    return "\n".join(parts)


def _parse_elicitation_reply(params: Any, reply_text: str) -> Any:
    """Convert a user reply into an MCP elicitation result."""
    from mcp import types

    text = reply_text.strip()
    lowered = text.lower()
    if lowered in {"cancel", "cancelled", "canceled", "/cancel"}:
        return types.ElicitResult(action="cancel")
    if lowered in {"decline", "declined", "deny", "no", "reject"}:
        return types.ElicitResult(action="decline")

    mode = str(getattr(params, "mode", "") or "").strip()
    if mode == "url":
        if lowered in {"confirm", "confirmed", "yes", "y", "ok", "open", "continue", "accept"}:
            return types.ElicitResult(action="accept")
        return types.ElicitResult(action="cancel")

    requested_schema = getattr(params, "requestedSchema", None)
    properties = {}
    if isinstance(requested_schema, Mapping):
        raw_properties = requested_schema.get("properties", {})
        if isinstance(raw_properties, Mapping):
            properties = dict(raw_properties)

    if not text:
        return types.ElicitResult(action="cancel")

    parsed_json: Any = None
    if text.startswith("{") or text.startswith("["):
        with suppress(json.JSONDecodeError, TypeError, ValueError):
            parsed_json = json.loads(text)

    if isinstance(parsed_json, dict):
        return types.ElicitResult(action="accept", content=parsed_json)

    if len(properties) == 1:
        key = next(iter(properties))
        return types.ElicitResult(action="accept", content={key: text})

    return types.ElicitResult(action="cancel")


def build_elicitation_callback(state: Any) -> Callable[[Any, Any], Any] | None:
    """Build an elicitation callback bound to the active agent loop state."""
    bus = getattr(state, "bus", None)
    pending_queues = getattr(state, "_pending_queues", None)
    if bus is None or not isinstance(pending_queues, Mapping):
        return None

    async def _elicitation_callback(context: Any, params: Any) -> Any:
        from mcp import types

        request_ctx = getattr(getattr(context, "session", None), _ACTIVE_REQUEST_CONTEXT_ATTR, None)
        if request_ctx is None:
            request_ctx = _CURRENT_REQUEST_CONTEXT.get()
        if request_ctx is None or not request_ctx.session_key:
            return types.ErrorData(
                code=types.INVALID_REQUEST,
                message="Elicitation requires an active request context.",
            )

        queue = pending_queues.get(request_ctx.session_key)
        if queue is None:
            return types.ErrorData(
                code=types.INVALID_REQUEST,
                message="No pending queue is available for elicitation.",
            )

        prompt = _format_elicitation_prompt(params)
        metadata = dict(request_ctx.metadata or {})
        metadata["_mcp_elicitation"] = {
            "elicitationId": getattr(params, "elicitationId", None),
            "mode": getattr(params, "mode", None),
        }
        if request_ctx.message_id:
            metadata.setdefault("message_id", request_ctx.message_id)

        await bus.publish_outbound(
            OutboundMessage(
                channel=request_ctx.channel,
                chat_id=request_ctx.chat_id,
                content=prompt,
                reply_to=request_ctx.message_id,
                metadata=metadata,
            )
        )

        try:
            inbound = await asyncio.wait_for(queue.get(), timeout=_ELICITATION_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "MCP elicitation timed out for session {}",
                request_ctx.session_key,
            )
            return types.ElicitResult(action="cancel")

        reply_text = str(getattr(inbound, "content", "") or "")
        result = _parse_elicitation_reply(params, reply_text)
        logger.debug(
            "MCP elicitation completed for session {} with action {}",
            request_ctx.session_key,
            getattr(result, "action", None),
        )
        return result

    return _elicitation_callback


async def _initialize_mcp_session(
    session: Any,
    *,
    elicitation_supported: bool = False,
) -> Any:
    """Initialize an MCP session while advertising the Tasks extension."""
    from mcp import types

    if not hasattr(session, "send_request") or not hasattr(session, "send_notification"):
        await session.initialize()
        return None

    task_handlers = getattr(session, "_task_handlers", None)
    tasks_capability = task_handlers.build_capability() if task_handlers is not None else None
    elicitation_capability = None
    if elicitation_supported:
        elicitation_capability = types.ElicitationCapability(
            form=types.FormElicitationCapability(),
            url=types.UrlElicitationCapability(),
        )
    tasks_extension_advertised = True
    tasks_typed_capability = tasks_capability is not None
    logger.info(
        "MCP initialize request: elicitation_supported={} tasks_extension_advertised={} tasks_typed_capability={}",
        elicitation_capability is not None,
        tasks_extension_advertised,
        tasks_typed_capability,
    )
    result = await session.send_request(
        types.ClientRequest(
            types.InitializeRequest(
                params=types.InitializeRequestParams(
                    protocolVersion=types.LATEST_PROTOCOL_VERSION,
                    capabilities=types.ClientCapabilities(
                        elicitation=elicitation_capability,
                        tasks=tasks_capability,
                    ),
                    _meta={_CLIENT_CAPABILITIES_META_KEY: _client_task_extensions()},
                    clientInfo=getattr(session, "_client_info", types.Implementation(name="mcp", version="0.1.0")),
                ),
            )
        ),
        types.InitializeResult,
    )

    supported_versions = {
        getattr(types, "LATEST_PROTOCOL_VERSION", result.protocolVersion),
        getattr(types, "DEFAULT_NEGOTIATED_VERSION", result.protocolVersion),
    }
    if result.protocolVersion not in supported_versions:
        raise RuntimeError(f"Unsupported protocol version from the server: {result.protocolVersion}")

    setattr(session, "_server_capabilities", result.capabilities)
    logger.info(
        "MCP initialize response: protocolVersion={} server_elicitation_supported={} server_tasks_supported={}",
        result.protocolVersion,
        getattr(result.capabilities, "elicitation", None) is not None,
        _supports_task_extension(result.capabilities),
    )
    await session.send_notification(types.ClientNotification(types.InitializedNotification()))
    return result


def _render_text_content(result: Any) -> str:
    """Render an MCP result content list into plain text."""
    from mcp import types

    parts: list[str] = []
    for block in getattr(result, "content", []):
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        else:
            parts.append(str(block))
    return "\n".join(parts) or "(no output)"

def _task_backend_kind_from_result(result: Any, session: Any) -> str:
    """Return the task backend kind implied by a tool call result."""
    result_type = getattr(result, "resultType", None)
    task = getattr(result, "task", None)
    task_id = getattr(task, "taskId", None)
    experimental = getattr(session, "experimental", None)
    has_task_lifecycle = (
        experimental is not None
        and callable(getattr(experimental, "get_task_result", None))
        and (
            callable(getattr(experimental, "poll_task", None))
            or callable(getattr(experimental, "get_task", None))
        )
    )
    if result_type == "task" and task_id and has_task_lifecycle:
        return _TASK_BACKEND_STANDARD
    return _TASK_BACKEND_NONE


def _task_lifecycle_available(session: Any) -> bool:
    """Return whether the installed SDK exposes task polling/result APIs."""
    experimental = getattr(session, "experimental", None)
    return bool(
        experimental is not None
        and callable(getattr(experimental, "get_task_result", None))
        and (
            callable(getattr(experimental, "poll_task", None))
            or callable(getattr(experimental, "get_task", None))
        )
    )


def _task_request_meta() -> dict[str, Any]:
    """Return per-request client capability metadata for task-aware calls."""
    return {_CLIENT_CAPABILITIES_META_KEY: _client_task_extensions()}


def _task_create_from_result(result: Any) -> str | None:
    """Extract a task id from a server-directed task result."""
    if getattr(result, "resultType", None) != "task":
        return None
    task = getattr(result, "task", None)
    task_id = getattr(task, "taskId", None)
    return str(task_id) if task_id else None


async def _task_poll_until_terminal(session: Any, task_id: str, backend_kind: str) -> Any:
    """Poll task status until a terminal state is reached."""
    if backend_kind not in {_TASK_BACKEND_STANDARD, _TASK_BACKEND_PROACTIVE}:
        return None

    experimental = getattr(session, "experimental", None)
    if experimental is None:
        return None
    poll_task = getattr(experimental, "poll_task", None)
    terminal_statuses = {
        "input_required",
        "completed",
        "failed",
        "cancelled",
    }

    if callable(poll_task):
        async for status in poll_task(task_id):
            if getattr(status, "status", None) in terminal_statuses:
                setattr(session, "_nanobot_mcp_last_task_status", {task_id: status})
                return status
        return None

    get_task = getattr(experimental, "get_task", None)
    if not callable(get_task):
        return None

    while True:
        status = await get_task(task_id)
        if getattr(status, "status", None) in terminal_statuses:
            setattr(session, "_nanobot_mcp_last_task_status", {task_id: status})
            return status
        interval_ms = (
            getattr(status, "pollIntervalMs", None)
            or getattr(status, "pollInterval", None)
            or 500
        )
        await asyncio.sleep(interval_ms / 1000)


async def _task_fetch_result(session: Any, task_id: str, backend_kind: str, result_type: Any) -> Any:
    """Fetch the final task result using the active backend."""
    if backend_kind not in {_TASK_BACKEND_STANDARD, _TASK_BACKEND_PROACTIVE}:
        return None
    experimental = getattr(session, "experimental", None)
    if experimental is None:
        return None
    return await experimental.get_task_result(task_id, result_type)


async def _task_cancel(session: Any, task_id: str, backend_kind: str) -> None:
    """Cancel an MCP task if the active backend supports it."""
    if backend_kind not in {_TASK_BACKEND_STANDARD, _TASK_BACKEND_PROACTIVE}:
        return None
    experimental = getattr(session, "experimental", None)
    if experimental is None:
        return None
    cancel_task = getattr(experimental, "cancel_task", None)
    if callable(cancel_task):
        await cancel_task(task_id)
    return None


class MCPToolWrapper(Tool, ContextAware):
    """Wraps a single MCP server tool as a nanobot Tool."""

    _plugin_discoverable = False

    def __init__(
        self,
        session,
        server_name: str,
        tool_def,
        tool_timeout: int = 30,
        *,
        task_extension_supported: bool = False,
    ):
        self._session = session
        self._original_name = tool_def.name
        self._name = _sanitize_name(f"mcp_{server_name}_{tool_def.name}")
        self._description = tool_def.description or tool_def.name
        raw_schema = tool_def.inputSchema or {"type": "object", "properties": {}}
        self._parameters = _normalize_schema_for_openai(raw_schema)
        self._task_support = _extract_tool_task_support(tool_def)
        self._task_extension_supported = task_extension_supported
        self._tool_timeout = tool_timeout
        self._request_context: RequestContext | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    def set_context(self, ctx: RequestContext) -> None:
        self._request_context = ctx

    @property
    def _supports_tasks(self) -> bool:
        if not (self._task_extension_supported and self._task_support in _TASK_SUPPORT_VALUES):
            return False
        experimental = getattr(self._session, "experimental", None)
        return bool(
            _task_lifecycle_available(self._session)
            and experimental is not None
            and callable(getattr(experimental, "call_tool_as_task", None))
        )

    async def _call_tool(self, kwargs: dict[str, Any]) -> Any:
        return await self._session.call_tool(self._original_name, arguments=kwargs)

    async def _call_tool_as_task(self, kwargs: dict[str, Any]) -> Any | None:
        experimental = getattr(self._session, "experimental", None)
        if experimental is None:
            return None
        call_tool_as_task = getattr(experimental, "call_tool_as_task", None)
        if not callable(call_tool_as_task):
            return None
        return await call_tool_as_task(
            self._original_name,
            kwargs,
            ttl=_TASK_DEFAULT_TTL_MS,
            meta=_task_request_meta(),
        )

    async def _call_tool_via_task_backend(
        self,
        kwargs: dict[str, Any] | None,
        *,
        backend_kind: str,
        create_result: Any,
    ) -> str:
        if backend_kind == _TASK_BACKEND_NONE:
            logger.warning(
                "MCP tool '{}' supports tasks, but no compatible task backend is available; falling back to call_tool()",
                self._name,
            )
            result = await self._call_tool(kwargs)
            return _render_text_content(result)

        from mcp import types

        logger.debug(
            "MCP task backend selected for tool '{}': backend_kind={} tool_task_support_declared={}",
            self._name,
            backend_kind,
            self._task_support,
        )

        task_id = (
            _task_create_from_result(create_result)
            if backend_kind == _TASK_BACKEND_STANDARD
            else str(getattr(getattr(create_result, "task", None), "taskId", "") or "")
        )
        if not task_id:
            if backend_kind == _TASK_BACKEND_PROACTIVE:
                return "(MCP task lifecycle unavailable)"
            return "(MCP task creation failed: missing task id)"

        status = await _task_poll_until_terminal(self._session, task_id, backend_kind)
        if status is None:
            return "(MCP task polling failed)"

        task_status = getattr(status, "status", None)
        if task_status in {"input_required", types.TASK_STATUS_COMPLETED}:
            result = await _task_fetch_result(
                self._session,
                task_id,
                backend_kind,
                types.CallToolResult,
            )
            if result is None:
                return "(MCP task result fetch failed)"
            return _render_text_content(result)
        if task_status in {types.TASK_STATUS_FAILED, types.TASK_STATUS_CANCELLED}:
            status_message = getattr(status, "statusMessage", None)
            if status_message:
                return f"(MCP task {task_status}: {status_message})"
            return f"(MCP task {task_status})"
        return f"(MCP task {task_status or 'unknown'})"

    async def _call_tool_or_task(self, kwargs: dict[str, Any]) -> str:
        if self._task_support in _TASK_SUPPORT_VALUES:
            if not self._task_extension_supported:
                logger.debug(
                    "MCP tool '{}' selected_path=plain tool_task_support_declared={} task_lifecycle_available={} server_tasks_supported=False",
                    self._name,
                    self._task_support,
                    _task_lifecycle_available(self._session),
                )
                result = await self._call_tool(kwargs)
                return _render_text_content(result)
            if not self._supports_tasks:
                logger.debug(
                    "MCP tool '{}' selected_path=proactive_task tool_task_support_declared={} task_lifecycle_available=False",
                    self._name,
                    self._task_support,
                )
                return "(MCP task lifecycle unavailable)"

            logger.debug(
                "MCP tool '{}' selected_path=proactive_task tool_task_support_declared={} task_lifecycle_available={}",
                self._name,
                self._task_support,
                _task_lifecycle_available(self._session),
            )
            create_result = await self._call_tool_as_task(kwargs)
            if create_result is None:
                return "(MCP task lifecycle unavailable)"
            return await self._call_tool_via_task_backend(
                None,
                backend_kind=_TASK_BACKEND_PROACTIVE,
                create_result=create_result,
            )

        result = await self._call_tool(kwargs)
        backend_kind = _task_backend_kind_from_result(result, self._session)
        if backend_kind == _TASK_BACKEND_STANDARD:
            logger.debug(
                "MCP tool '{}' selected_path=server_directed_task tool_task_support_declared={} task_lifecycle_available={}",
                self._name,
                self._task_support,
                _task_lifecycle_available(self._session),
            )
            return await self._call_tool_via_task_backend(
                kwargs,
                backend_kind=_TASK_BACKEND_STANDARD,
                create_result=result,
            )

        logger.debug(
            "MCP tool '{}' selected_path=plain tool_task_support_declared={} task_lifecycle_available={}",
            self._name,
            self._task_support,
            _task_lifecycle_available(self._session),
        )
        return _render_text_content(result)

    async def execute(self, **kwargs: Any) -> str:
        attempts = 1 if self._supports_tasks else 2
        request_ctx = self._request_context
        token = _CURRENT_REQUEST_CONTEXT.set(request_ctx)
        previous_request_ctx = getattr(self._session, _ACTIVE_REQUEST_CONTEXT_ATTR, None)
        setattr(self._session, _ACTIVE_REQUEST_CONTEXT_ATTR, request_ctx)
        try:
            for attempt in range(attempts):
                try:
                    result = await asyncio.wait_for(
                        self._call_tool_or_task(kwargs),
                        timeout=self._tool_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "MCP tool '{}' timed out after {}s", self._name, self._tool_timeout
                    )
                    return f"(MCP tool call timed out after {self._tool_timeout}s)"
                except asyncio.CancelledError:
                    # MCP SDK's anyio cancel scopes can leak CancelledError on timeout/failure.
                    # Re-raise only if our task was externally cancelled (e.g. /stop).
                    task = asyncio.current_task()
                    if task is not None and task.cancelling() > 0:
                        raise
                    logger.warning("MCP tool '{}' was cancelled by server/SDK", self._name)
                    return "(MCP tool call was cancelled)"
                except Exception as exc:
                    if _is_transient(exc):
                        if attempt + 1 < attempts:
                            logger.warning(
                                "MCP tool '{}' hit transient error ({}), retrying once...",
                                self._name,
                                type(exc).__name__,
                            )
                            await asyncio.sleep(1)  # Brief backoff before retry
                            continue
                        # No more retries remain.
                        logger.exception(
                            "MCP tool '{}' failed: {}",
                            self._name,
                            type(exc).__name__,
                        )
                        return f"(MCP tool call failed: {type(exc).__name__})"
                    logger.exception(
                        "MCP tool '{}' failed: {}: {}",
                        self._name,
                        type(exc).__name__,
                        exc,
                    )
                    return f"(MCP tool call failed: {type(exc).__name__})"
                else:
                    return result

            return "(MCP tool call failed)"  # Unreachable, but satisfies type checkers
        finally:
            if previous_request_ctx is None:
                with suppress(AttributeError):
                    delattr(self._session, _ACTIVE_REQUEST_CONTEXT_ATTR)
            else:
                setattr(self._session, _ACTIVE_REQUEST_CONTEXT_ATTR, previous_request_ctx)
            _CURRENT_REQUEST_CONTEXT.reset(token)


class MCPResourceWrapper(Tool):
    """Wraps an MCP resource URI as a read-only nanobot Tool."""

    _plugin_discoverable = False

    def __init__(self, session, server_name: str, resource_def, resource_timeout: int = 30):
        self._session = session
        self._uri = resource_def.uri
        self._name = _sanitize_name(f"mcp_{server_name}_resource_{resource_def.name}")
        desc = resource_def.description or resource_def.name
        self._description = f"[MCP Resource] {desc}\nURI: {self._uri}"
        self._parameters: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "required": [],
        }
        self._resource_timeout = resource_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types

        for attempt in range(2):
            try:
                result = await asyncio.wait_for(
                    self._session.read_resource(self._uri),
                    timeout=self._resource_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP resource '{}' timed out after {}s", self._name, self._resource_timeout
                )
                return f"(MCP resource read timed out after {self._resource_timeout}s)"
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning("MCP resource '{}' was cancelled by server/SDK", self._name)
                return "(MCP resource read was cancelled)"
            except Exception as exc:
                if _is_transient(exc):
                    if attempt == 0:
                        logger.warning(
                            "MCP resource '{}' hit transient error ({}), retrying once...",
                            self._name,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(1)
                        continue
                    logger.exception(
                        "MCP resource '{}' failed after retry: {}",
                        self._name,
                        type(exc).__name__,
                    )
                    return f"(MCP resource read failed after retry: {type(exc).__name__})"
                logger.exception(
                    "MCP resource '{}' failed: {}: {}",
                    self._name,
                    type(exc).__name__,
                    exc,
                )
                return f"(MCP resource read failed: {type(exc).__name__})"
            else:
                parts: list[str] = []
                for block in result.contents:
                    if isinstance(block, types.TextResourceContents):
                        parts.append(block.text)
                    elif isinstance(block, types.BlobResourceContents):
                        parts.append(f"[Binary resource: {len(block.blob)} bytes]")
                    else:
                        parts.append(str(block))
                return "\n".join(parts) or "(no output)"

        return "(MCP resource read failed)"  # Unreachable


class MCPPromptWrapper(Tool):
    """Wraps an MCP prompt as a read-only nanobot Tool."""

    _plugin_discoverable = False

    def __init__(self, session, server_name: str, prompt_def, prompt_timeout: int = 30):
        self._session = session
        self._prompt_name = prompt_def.name
        self._name = _sanitize_name(f"mcp_{server_name}_prompt_{prompt_def.name}")
        desc = prompt_def.description or prompt_def.name
        self._description = (
            f"[MCP Prompt] {desc}\n"
            "Returns a filled prompt template that can be used as a workflow guide."
        )
        self._prompt_timeout = prompt_timeout

        # Build parameters from prompt arguments
        properties: dict[str, Any] = {}
        required: list[str] = []
        for arg in prompt_def.arguments or []:
            prop: dict[str, Any] = {"type": "string"}
            if getattr(arg, "description", None):
                prop["description"] = arg.description
            properties[arg.name] = prop
            if arg.required:
                required.append(arg.name)
        self._parameters: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types
        from mcp.shared.exceptions import McpError

        for attempt in range(2):
            try:
                result = await asyncio.wait_for(
                    self._session.get_prompt(self._prompt_name, arguments=kwargs),
                    timeout=self._prompt_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP prompt '{}' timed out after {}s", self._name, self._prompt_timeout
                )
                return f"(MCP prompt call timed out after {self._prompt_timeout}s)"
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning("MCP prompt '{}' was cancelled by server/SDK", self._name)
                return "(MCP prompt call was cancelled)"
            except McpError as exc:
                logger.exception(
                    "MCP prompt '{}' failed: code={} message={}",
                    self._name,
                    exc.error.code,
                    exc.error.message,
                )
                return f"(MCP prompt call failed: {exc.error.message} [code {exc.error.code}])"
            except Exception as exc:
                if _is_transient(exc):
                    if attempt == 0:
                        logger.warning(
                            "MCP prompt '{}' hit transient error ({}), retrying once...",
                            self._name,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(1)
                        continue
                    logger.exception(
                        "MCP prompt '{}' failed after retry: {}",
                        self._name,
                        type(exc).__name__,
                    )
                    return f"(MCP prompt call failed after retry: {type(exc).__name__})"
                logger.exception(
                    "MCP prompt '{}' failed: {}: {}",
                    self._name,
                    type(exc).__name__,
                    exc,
                )
                return f"(MCP prompt call failed: {type(exc).__name__})"
            else:
                parts: list[str] = []
                for message in result.messages:
                    content = message.content
                    if isinstance(content, types.TextContent):
                        parts.append(content.text)
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, types.TextContent):
                                parts.append(block.text)
                            else:
                                parts.append(str(block))
                    else:
                        parts.append(str(content))
                return "\n".join(parts) or "(no output)"

        return "(MCP prompt call failed)"  # Unreachable


async def connect_mcp_servers(
    mcp_servers: dict,
    registry: ToolRegistry,
    *,
    elicitation_callback: Callable[[Any, Any], Any] | None = None,
) -> dict[str, AsyncExitStack]:
    """Connect to configured MCP servers and register their tools, resources, prompts.

    Returns a dict mapping server name -> its dedicated AsyncExitStack.
    Each server gets its own stack to prevent cancel scope conflicts
    when multiple MCP servers are configured.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    async def connect_single_server(name: str, cfg) -> tuple[str, AsyncExitStack | None]:
        server_stack = AsyncExitStack()
        await server_stack.__aenter__()

        try:
            transport_type = cfg.type
            if not transport_type:
                if cfg.command:
                    transport_type = "stdio"
                elif cfg.url:
                    transport_type = (
                        "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
                    )
                else:
                    logger.warning("MCP server '{}': no command or url configured, skipping", name)
                    await server_stack.aclose()
                    return name, None

            if transport_type == "stdio":
                command, args, env = _normalize_windows_stdio_command(
                    cfg.command,
                    cfg.args,
                    cfg.env or None,
                )
                params = StdioServerParameters(
                    command=command,
                    args=args,
                    env=env,
                    cwd=cfg.cwd or None,
                )
                read, write = await server_stack.enter_async_context(stdio_client(params))
            elif transport_type == "sse":
                if not await _probe_http_url(cfg.url):
                    logger.warning("MCP server '{}': {} unreachable, skipping", name, cfg.url)
                    await server_stack.aclose()
                    return name, None

                def httpx_client_factory(
                    headers: dict[str, str] | None = None,
                    timeout: httpx.Timeout | None = None,
                    auth: httpx.Auth | None = None,
                ) -> httpx.AsyncClient:
                    merged_headers = {
                        "Accept": "application/json, text/event-stream",
                        **(cfg.headers or {}),
                        **(headers or {}),
                    }
                    return httpx.AsyncClient(
                        headers=merged_headers or None,
                        follow_redirects=True,
                        timeout=timeout,
                        auth=auth,
                    )

                read, write = await server_stack.enter_async_context(
                    sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
                )
            elif transport_type == "streamableHttp":
                if not await _probe_http_url(cfg.url):
                    logger.warning("MCP server '{}': {} unreachable, skipping", name, cfg.url)
                    await server_stack.aclose()
                    return name, None

                http_client = await server_stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=cfg.headers or None,
                        follow_redirects=True,
                        timeout=None,
                    )
                )
                read, write, _ = await server_stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=http_client)
                )
            else:
                logger.warning("MCP server '{}': unknown transport type '{}'", name, transport_type)
                await server_stack.aclose()
                return name, None

            session = await server_stack.enter_async_context(
                ClientSession(read, write, elicitation_callback=elicitation_callback)
            )
            logger.info(
                "MCP server '{}': session created (transport={}, elicitation_callback={}, task_lifecycle_available={})",
                name,
                transport_type,
                elicitation_callback is not None,
                _task_lifecycle_available(session),
            )
            await _initialize_mcp_session(
                session,
                elicitation_supported=elicitation_callback is not None,
            )
            get_server_capabilities = getattr(session, "get_server_capabilities", None)
            task_extension_supported = _supports_task_extension(
                get_server_capabilities() if callable(get_server_capabilities) else None
            )

            tools = await session.list_tools()
            enabled_tools = set(cfg.enabled_tools)
            allow_all_tools = "*" in enabled_tools
            registered_count = 0
            matched_enabled_tools: set[str] = set()
            available_raw_names = [tool_def.name for tool_def in tools.tools]
            available_wrapped_names = [_sanitize_name(f"mcp_{name}_{tool_def.name}") for tool_def in tools.tools]
            for tool_def in tools.tools:
                wrapped_name = _sanitize_name(f"mcp_{name}_{tool_def.name}")
                if (
                    not allow_all_tools
                    and tool_def.name not in enabled_tools
                    and wrapped_name not in enabled_tools
                ):
                    logger.debug(
                        "MCP: skipping tool '{}' from server '{}' (not in enabledTools)",
                        wrapped_name,
                        name,
                    )
                    continue
                wrapper = MCPToolWrapper(
                    session,
                    name,
                    tool_def,
                    tool_timeout=cfg.tool_timeout,
                    task_extension_supported=task_extension_supported,
                )
                registry.register(wrapper)
                logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, name)
                registered_count += 1
                if enabled_tools:
                    if tool_def.name in enabled_tools:
                        matched_enabled_tools.add(tool_def.name)
                    if wrapped_name in enabled_tools:
                        matched_enabled_tools.add(wrapped_name)

            if enabled_tools and not allow_all_tools:
                unmatched_enabled_tools = sorted(enabled_tools - matched_enabled_tools)
                if unmatched_enabled_tools:
                    logger.warning(
                        "MCP server '{}': enabledTools entries not found: {}. Available raw names: {}. "
                        "Available wrapped names: {}",
                        name,
                        ", ".join(unmatched_enabled_tools),
                        ", ".join(available_raw_names) or "(none)",
                        ", ".join(available_wrapped_names) or "(none)",
                    )

            try:
                resources_result = await session.list_resources()
                for resource in resources_result.resources:
                    wrapper = MCPResourceWrapper(
                        session, name, resource, resource_timeout=cfg.tool_timeout
                    )
                    registry.register(wrapper)
                    registered_count += 1
                    logger.debug(
                        "MCP: registered resource '{}' from server '{}'", wrapper.name, name
                    )
            except Exception as e:
                logger.debug("MCP server '{}': resources not supported or failed: {}", name, e)

            try:
                prompts_result = await session.list_prompts()
                for prompt in prompts_result.prompts:
                    wrapper = MCPPromptWrapper(
                        session, name, prompt, prompt_timeout=cfg.tool_timeout
                    )
                    registry.register(wrapper)
                    registered_count += 1
                    logger.debug("MCP: registered prompt '{}' from server '{}'", wrapper.name, name)
            except Exception as e:
                logger.debug("MCP server '{}': prompts not supported or failed: {}", name, e)

            logger.info(
                "MCP server '{}': connected, {} capabilities registered", name, registered_count
            )
            return name, server_stack

        except Exception as e:
            hint = ""
            text = str(e).lower()
            if any(
                marker in text
                for marker in (
                    "parse error",
                    "invalid json",
                    "unexpected token",
                    "jsonrpc",
                    "content-length",
                )
            ):
                hint = (
                    " Hint: this looks like stdio protocol pollution. Make sure the MCP server writes "
                    "only JSON-RPC to stdout and sends logs/debug output to stderr instead."
                )
            logger.exception("MCP server '{}': failed to connect: {}", name, hint)
            with suppress(Exception):
                await server_stack.aclose()
            return name, None

    server_stacks: dict[str, AsyncExitStack] = {}

    for name, cfg in mcp_servers.items():
        try:
            result = await connect_single_server(name, cfg)
        except Exception as e:
            logger.exception("MCP server '{}' connection failed: {}", name, e)
            continue
        if result is not None and result[1] is not None:
            server_stacks[result[0]] = result[1]

    return server_stacks


def session_extra(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return persisted session kwargs for MCP preset attachments."""
    mcp_presets = metadata.get("mcp_presets") if isinstance(metadata, Mapping) else None
    return {"mcp_presets": mcp_presets} if isinstance(mcp_presets, list) and mcp_presets else {}


def runtime_lines(
    message: Any,
    *,
    available_server_names: set[str] | None = None,
    configured_server_names: set[str] | None = None,
    connected_server_names: set[str] | None = None,
    skip: bool = False,
) -> list[str]:
    """Return model-visible MCP preset annotations for the current turn."""
    if skip:
        return []
    if configured_server_names is None:
        configured_server_names = available_server_names
    if connected_server_names is None:
        connected_server_names = available_server_names
    metadata = message.metadata if isinstance(getattr(message, "metadata", None), Mapping) else None
    structured = metadata.get("mcp_presets") if isinstance(metadata, Mapping) else None
    if not isinstance(structured, list):
        return []

    lines: list[str] = []
    for item in structured[:8]:
        if not isinstance(item, Mapping):
            continue
        raw_name = str(item.get("name") or "").strip().lower()
        if not raw_name:
            continue
        display = str(item.get("display_name") or raw_name).strip() or raw_name
        transport = str(item.get("transport") or "mcp").strip() or "mcp"
        prefix = f"mcp_{raw_name}_"
        if configured_server_names is not None and raw_name not in configured_server_names:
            lines.append(
                "MCP Preset Attachment: "
                f"@{raw_name} ({display}; transport={transport}) is configured in WebUI Settings, "
                "but this gateway has not loaded the latest MCP settings yet. "
                f"Tools with prefix `{prefix}` may not be available yet; if they are missing, "
                "tell the user to restart nanobot."
            )
            continue
        if connected_server_names is not None and raw_name not in connected_server_names:
            lines.append(
                "MCP Preset Attachment: "
                f"@{raw_name} ({display}; transport={transport}) is configured, "
                "but its MCP connection is not currently live. "
                f"Tools with prefix `{prefix}` may be unavailable; tell the user to open Settings, "
                "run the preset test, and restart nanobot only if hot reload is unavailable."
            )
            continue
        lines.append(
            "MCP Preset Attachment: "
            f"@{raw_name} ({display}; transport={transport}; tool_prefix={prefix}). "
            f"Prefer available tools whose names start with `{prefix}` for this request; "
            "do not substitute shell commands for this MCP integration unless the user asks."
        )
    return lines


async def connect_missing_servers(state: Any, registry: ToolRegistry) -> None:
    """Connect configured MCP servers that are not currently live."""
    missing_servers = {
        name: cfg for name, cfg in state._mcp_servers.items() if name not in state._mcp_stacks
    }
    if state._mcp_connecting or not missing_servers:
        return
    state._mcp_connecting = True
    try:
        connected = await connect_mcp_servers(
            missing_servers,
            registry,
            elicitation_callback=build_elicitation_callback(state),
        )
        state._mcp_stacks.update(connected)
        state._mcp_connected = bool(state._mcp_stacks)
        if connected:
            logger.info("MCP connected servers: {}", sorted(connected))
        else:
            logger.warning("No MCP servers connected successfully (will retry next message)")
    except asyncio.CancelledError:
        logger.warning("MCP connection cancelled (will retry next message)")
        state._mcp_connected = bool(state._mcp_stacks)
    except BaseException as e:
        logger.warning("Failed to connect MCP servers (will retry next message): {}", e)
        state._mcp_connected = bool(state._mcp_stacks)
    finally:
        state._mcp_connecting = False


async def reload_servers(state: Any, registry: ToolRegistry) -> dict[str, Any]:
    """Reconcile live MCP connections with the current config file."""
    async with _reload_lock(state):
        try:
            from nanobot.config.loader import load_config, resolve_config_env_vars

            config = resolve_config_env_vars(load_config())
            next_servers = dict(config.tools.mcp_servers)
        except Exception as exc:
            logger.warning("MCP hot reload could not read config: {}", exc)
            return {
                "ok": False,
                "message": "Could not reload MCP config. Restart nanobot to pick up changes.",
                "requires_restart": True,
                "error": str(exc),
            }

        current_servers = dict(state._mcp_servers)
        current_names = set(current_servers)
        next_names = set(next_servers)
        removed = sorted(current_names - next_names)
        added = sorted(next_names - current_names)
        changed = sorted(
            name
            for name in current_names & next_names
            if _server_signature(current_servers[name]) != _server_signature(next_servers[name])
        )

        tools_removed = 0
        for name in [*removed, *changed]:
            tools_removed += _unregister_server_tools(state, registry, name)
            await _close_server(state, name)

        state._mcp_servers = next_servers
        retry_missing = sorted(
            name
            for name in next_names
            if name not in state._mcp_stacks and name not in set(added) | set(changed)
        )
        to_connect_names = sorted(set(added) | set(changed) | set(retry_missing))
        to_connect = {name: next_servers[name] for name in to_connect_names}
        connected: dict[str, AsyncExitStack] = {}
        if to_connect:
            connected = await connect_mcp_servers(
                to_connect,
                registry,
                elicitation_callback=build_elicitation_callback(state),
            )
            state._mcp_stacks.update(connected)

        state._mcp_connected = bool(state._mcp_stacks)
        failed = sorted(set(to_connect) - set(connected))
        unchanged = not removed and not added and not changed and not retry_missing
        ok = not failed
        if failed:
            message = "MCP config reloaded, but some servers did not connect: " + ", ".join(failed)
        elif unchanged:
            message = "MCP config is already live."
        elif retry_missing and not added and not changed and not removed:
            message = "MCP connections refreshed without restarting nanobot."
        else:
            message = "MCP config reloaded without restarting nanobot."

        logger.info(
            "MCP hot reload: added={} changed={} removed={} retried={} connected={} failed={} tools_removed={}",
            added,
            changed,
            removed,
            retry_missing,
            sorted(connected),
            failed,
            tools_removed,
        )
        return {
            "ok": ok,
            "message": message,
            "added": added,
            "changed": changed,
            "removed": removed,
            "retried": retry_missing,
            "connected": sorted(state._mcp_stacks),
            "configured": sorted(state._mcp_servers),
            "failed": failed,
            "tools_removed": tools_removed,
            "requires_restart": False,
        }


async def request_mcp_reload(bus: Any, *, timeout: float = 15.0) -> dict[str, Any]:
    """Ask the running agent loop to reconcile live MCP connections."""
    loop = asyncio.get_running_loop()
    ack: asyncio.Future[dict[str, Any]] = loop.create_future()
    await bus.publish_inbound(
        InboundMessage(
            channel="system",
            sender_id="webui-settings",
            chat_id="runtime",
            content=RUNTIME_CONTROL_MCP_RELOAD,
            metadata={
                INBOUND_META_RUNTIME_CONTROL: RUNTIME_CONTROL_MCP_RELOAD,
                RUNTIME_CONTROL_ACK: ack,
            },
        )
    )
    try:
        result = await asyncio.wait_for(ack, timeout=timeout)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "message": "MCP hot reload timed out. Restart nanobot to pick up changes.",
            "requires_restart": True,
        }
    return result if isinstance(result, dict) else {
        "ok": False,
        "message": "MCP hot reload returned an unexpected response.",
        "requires_restart": True,
    }


async def handle_runtime_control(state: Any, msg: InboundMessage, registry: ToolRegistry) -> bool:
    metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
    control = metadata.get(INBOUND_META_RUNTIME_CONTROL)
    if control != RUNTIME_CONTROL_MCP_RELOAD:
        return False

    ack = metadata.get(RUNTIME_CONTROL_ACK)
    try:
        result = await reload_servers(state, registry)
    except Exception as exc:
        logger.exception("MCP hot reload failed")
        result = {
            "ok": False,
            "message": "MCP hot reload failed. Restart nanobot to pick up changes.",
            "requires_restart": True,
            "error": str(exc),
        }
    if isinstance(ack, asyncio.Future) and not ack.done():
        ack.set_result(result)
    return True


def _reload_lock(state: Any) -> asyncio.Lock:
    try:
        return _RELOAD_LOCKS[state]
    except KeyError:
        lock = asyncio.Lock()
        _RELOAD_LOCKS[state] = lock
        return lock


def _server_signature(cfg: Any) -> Any:
    if hasattr(cfg, "model_dump"):
        return cfg.model_dump(mode="json")
    return cfg


def _tool_prefix(server_name: str) -> str:
    safe_name = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in server_name)
    while "__" in safe_name:
        safe_name = safe_name.replace("__", "_")
    return f"mcp_{safe_name}_"


def _unregister_server_tools(state: Any, registry: ToolRegistry, server_name: str) -> int:
    prefix = _tool_prefix(server_name)
    removed = 0
    for tool_name in list(registry.tool_names):
        if tool_name.startswith(prefix):
            registry.unregister(tool_name)
            removed += 1
    return removed


async def _close_server(state: Any, server_name: str) -> None:
    stack = state._mcp_stacks.pop(server_name, None)
    if stack is None:
        return
    try:
        await stack.aclose()
    except (RuntimeError, BaseExceptionGroup):
        logger.debug("MCP server '{}' cleanup error (can be ignored)", server_name)
