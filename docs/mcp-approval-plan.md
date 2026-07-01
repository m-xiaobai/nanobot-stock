# MCP 敏感操作确认方案

## Summary
本方案为 nanobot 中的 MCP tool 调用增加一层宿主侧的敏感操作审批 gate。
审批主要依赖标准 `ToolAnnotations`，尤其是 `destructiveHint` 和 `readOnlyHint`，不使用
`_meta["nanobot"]` 作为审批判断依据。

飞书是 v1 的优先渠道。其主要交互路径是带 `批准` / `拒绝` 按钮的交互式卡片，
按钮回调会返回包含 `approval_id` 的结构化 payload。纯文本确认仅保留为兜底路径，
并且必须使用显式命令格式，例如 `/approve approval-123`。

本方案有意将审批与 MCP elicitation 分开：

- approval gate 回答的是“这个 tool 调用是否允许执行”
- elicitation 回答的是“server 还缺什么额外输入”

这种职责分离能让执行链更简单，也能让 nanobot 在任何远端副作用发生前先拦住 destructive 调用。

## 为什么优先采用 Approval Gate，而不是 Elicitation
- 敏感操作确认本质上是宿主侧安全策略，不是 server 的输入补全流程。
- client 可以在 `call_tool()` 或 `call_tool_as_task()` 发出前直接拦截。
- 用户拒绝或超时后，远端操作完全不会运行。
- 该设计不要求所有 MCP server 都实现 `elicitation/create`。
- 审批状态机很小：`pending -> approved / declined / timed out -> resume / abort`。

## 职责划分
### MCP Server
- 在 `list_tools()` 中暴露标准 `ToolAnnotations`
- 对可能执行 destructive 更新的工具设置 `destructiveHint`
- 对只读工具设置 `readOnlyHint`
- 仍可保留自身的 destructive approval 或 scope 校验作为最终防线

### nanobot MCP Client
- 在注册 MCP tool 时读取 `ToolAnnotations`
- 在执行前判断该调用是否需要审批
- 创建 pending approval，发送确认提示，并等待用户回复
- 只有审批通过后，才恢复原始 MCP 调用协程

## 审批判定规则
审批判断顺序如下：

1. 本地针对 `server_name + tool_name` 的覆盖规则
2. `destructiveHint == true` -> `require_approval`
3. `readOnlyHint == true` -> `allow`
4. annotations 缺失时，回退到本地启发式规则

审批结果只允许三种：

- `allow`
- `require_approval`
- `deny`

## 用户交互
### 飞书主路径
- 宿主发送一张飞书交互卡片
- 卡片内容包含：
  - MCP server 名
  - tool 名
  - 截断后的参数摘要
  - `批准` 按钮
  - `拒绝` 按钮
- 每个按钮都会回传一个结构化回调，至少包含：
  - `approval_id`
  - `action=approve|decline`
  - `session_key`

### 文本兜底路径
- 只有当按钮回调不可用时，才使用文本确认
- 宿主会提示用户回复：
  - `/approve <approval_id>`
  - `/decline <approval_id>`
- 裸 `approve` / `decline` 不再视为有效审批回复

回复处理结果：

- `approve` -> 执行原始 MCP 调用
- `decline` -> 返回 `(MCP tool call declined by user approval policy)`
- `timeout` -> 返回 `(MCP tool call approval timed out)`
- 本地 `deny` -> 返回 `(MCP tool call denied by local approval policy)`

这些结果都作为普通 tool 结果返回，而不是抛出系统异常。

## 并发与路由
为了避免回复歧义，v1 仍然限制为每个 `session_key` 只允许一个 active approval。

- 如果同一 session 中已有一个 pending approval，再来第二个敏感调用时，直接拒绝注册
- 审批回复会在普通 agent turn 分发前优先消费
- 命中的审批回复会直接恢复等待中的 MCP tool 协程
- 匹配必须是显式的：
  - 先按结构化回调中的 `approval_id`
  - 再按显式文本命令 `/approve <approval_id>` 或 `/decline <approval_id>`
  - 没有明确目标的信息，不会被当作审批回复

这意味着审批不会创建新的 agent turn，也不会进入普通 LLM 循环。

## 失败路径
- annotations 缺失：退回本地启发式规则
- approval callback 不可用：安全地拒绝调用
- 用户拒绝：直接中止，不发送远端请求
- 超时：直接中止，不发送远端请求
- client 已审批但 server 仍拒绝：按正常 server 错误向上返回

## Server 说明
对于 `mcp_stock_server`，`ToolDefinition.destructive` 继续作为内部风险真相。
server 在 `list_tools()` 时把该字段映射成标准 `ToolAnnotations`。

## 测试覆盖
- server 正确暴露 `destructiveHint` 和 `readOnlyHint`
- client 能正确读取 annotations
- destructive tool 调用会进入 approval gate
- read-only 工具默认跳过审批
- 只有审批通过后，远端调用才会真正执行
- 用户拒绝或超时后，远端调用永远不会执行
- 审批回复不会进入普通 agent turn
