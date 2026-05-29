# 基于 nanobot 的 A 股智能选股助手架构设计

## 文档信息

- 作者：Codex
- 状态：草案
- 最后更新：2026-05-28
- 范围：基于 nanobot 的 A 股智能选股 MVP

## 1. 背景与目标

### 1.1 背景

本项目旨在基于 `nanobot` 二次开发一个面向 A 股场景的智能选股助手。系统目标不是通用聊天，也不是自动交易，而是一条面向投研日报的结构化工作流：从 A 股股票池中按策略筛选候选股票、过滤明显负面风险、进行技术面评分，并生成可阅读、可追踪的候选股报告。

本设计尽量复用 `nanobot` 现有能力：

- `AgentLoop` 作为主运行时
- `SubagentManager` 作为阶段执行器
- Slash Command 作为人工触发入口
- `CronService` 作为后续自动日报的调度能力
- Channel / WebUI 作为结果交付层

### 1.2 目标

- 在 `nanobot` 之上构建一条面向 A 股的多 subagent 选股工作流
- 保证每个阶段职责明确、输入输出稳定、可被机器消费
- 生成结构化的 `DailySelectionReport`
- 将 prompt 与 orchestration 解耦，便于后续独立维护
- 为后续接入真实 MCP 行情能力和新闻能力预留清晰扩展点

### 1.3 非目标

- 自动下单或交易执行
- 回测系统
- 开放式主观投顾问答
- 第一版自然语言随意定义策略
- 在 orchestrator 中写死外部数据源协议细节

## 2. 总体架构

### 2.1 架构分层

系统分为四层：

1. 接入层  
   包含 CLI、WebUI 和聊天渠道。当前统一入口为：

   ```text
   /stock-report <strategy> [YYYY-MM-DD]
   ```

2. 编排层  
   由 `StockSelectionSubagentOrchestrator` 负责：
   - 顺序调度阶段 subagent
   - 校验阶段 JSON 输出
   - 汇总最终日报

3. 分析层  
   包含四个阶段 subagent：
   1. `stock-screening`
   2. `news-filter`
   3. `market-scoring`
   4. `report-summary`

4. 数据能力层  
   负责接入：
   - MCP server 提供的 A 股股票池、行情、指标能力
   - 新闻工具 / skill
   - 工作区 `skills/` 中的策略 skill

### 2.2 核心运行时组件

| 组件 | 职责 | 位置 |
|---|---|---|
| `AgentLoop` | 持有运行时状态与命令分发 | `nanobot/agent/loop.py` |
| `cmd_stock_report` | 面向用户的命令入口 | `nanobot/command/builtin.py` |
| `StockSelectionSubagentOrchestrator` | 执行选股主流程 | `nanobot/stocks/orchestrator.py` |
| `SubagentManager.run_inline` | 前台串行执行阶段 subagent | `nanobot/agent/subagent.py` |
| Prompt 模板 | 阶段化 system/task prompt | `nanobot/templates/stocks/` |

## 3. 四阶段工作流设计

### 3.1 触发方式

当前主触发方式为：

```text
/stock-report B1
/stock-report B1 2026-05-26
```

调用链如下：

```text
/stock-report
-> cmd_stock_report()
-> loop.stock_selection_orchestrator.run_daily_stock_selection()
-> orchestrator._run_json_stage()
-> subagents.run_inline()
-> AgentRunner.run()
-> 阶段 JSON 结果
-> orchestrator 汇总
-> DailySelectionReport
-> 渲染为最终文本回复
```

### 3.2 为什么采用前台串行 subagent

原始的 `SubagentManager.spawn()` 更适合后台独立任务：子 agent 运行完成后通过消息总线回注结果。  
而选股日报流程是强顺序依赖的流水线，每个阶段都必须立即消费上一个阶段的输出，因此更适合采用 `SubagentManager.run_inline()`。

这种方式的优点是：

- 保留真实 subagent 执行
- 每个阶段仍有独立 prompt 与工具上下文
- 结果直接返回给 orchestrator
- 易于按阶段调试与校验

## 4. 四阶段 Subagent 拓扑

系统包含 `1 个 orchestrator + 4 个阶段 subagent`。

### 4.1 Orchestrator

**组件**：`StockSelectionSubagentOrchestrator`

**职责：**

- 校验 `strategy_name`
- 顺序调用四个阶段 subagent
- 解析并校验每个阶段的 JSON 输出
- 合并中间结果为最终 `DailySelectionReport`
- 记录外部依赖失败的 `partial_failures`

**不负责：**

- 直接做金融分析
- 直接读取策略 skill 内容
- 直接访问外部数据协议细节
- 输出长篇自由文本分析

### 4.2 阶段一：`stock-screening`

这是整个工作流的核心阶段，也是合并后的第一阶段。

**职责：**

- 从 MCP server 获取 A 股全量股票代码
- 获取策略所需行情、指标或其他结构化市场数据
- 根据 `strategy_name` 定位工作区中的策略 skill
- **自行读取**工作区 skill 内容
- 按该策略执行筛选
- 输出通过筛选的股票列表

**输入：**

- `trade_date`
- `strategy_name`

**输出契约：**

```json
{
  "items": [
    {
      "symbol": "600001",
      "strategy_name": "B1",
      "screen_pass_reasons": ["close broke above the recent range high"],
      "risk_notes": []
    }
  ]
}
```

**建议可选统计字段：**

```json
{
  "screened_count": 5300,
  "passed_count": 18,
  "screening_notes": ["screening completed with full market coverage"]
}
```

**边界：**

- 不接收上游 `symbols`
- 不逐条返回筛选失败股票
- 不做新闻过滤
- 不做技术评分
- 不写最终日报摘要

### 4.3 阶段二：`news-filter`

**职责：**

- 对筛选通过的股票做重大负面新闻过滤
- 标记风险或剔除明显不应进入下一阶段的股票

**输入：**

- `symbols`

**输出契约：**

```json
{
  "items": [
    {
      "symbol": "600001",
      "allowed": true,
      "negative_news_flags": [],
      "risk_notes": []
    }
  ],
  "partial_failures": []
}
```

**边界：**

- 只判断重大新闻风险
- 不做技术评分
- 不输出最终摘要

### 4.4 阶段三：`market-scoring`

**职责：**

- 对通过新闻过滤的股票做技术面评分

**输入：**

- `symbols`

**输出契约：**

```json
{
  "items": [
    {
      "symbol": "600001",
      "technical_score": 90,
      "score_reasons": ["trend is above the short and medium moving averages"],
      "risk_notes": ["watch for next-day follow-through"]
    }
  ]
}
```

**边界：**

- 只做技术评分
- 不重建股票池
- 不做新闻判断
- 不输出最终摘要

### 4.5 阶段四：`report-summary`

**职责：**

- 生成日报摘要与全局免责声明

**输入：**

- `selected`
- 可选 `screening_stats`

**输出契约：**

```json
{
  "summary": "Selected candidates: 600001.",
  "global_risk_disclaimer": "For research use only. This report is not investment advice."
}
```

**边界：**

- 不重新分析股票
- 不生成新的业务事实
- 只消费前面阶段已经确定的结果

## 5. `stock-screening` 阶段设计

### 5.1 阶段定位

`stock-screening` 是合并后的第一阶段，用于统一承接：

- 股票池来源
- 策略载入
- 初筛执行

它替代了旧方案中的：

- `stock-universe`
- 依赖上游 `symbols` 输入的 `stock-screening`

### 5.2 输入与处理流程

该阶段在收到：

- `trade_date`
- `strategy_name`

后，内部执行以下逻辑：

1. 通过 MCP server 获取 A 股全量股票代码
2. 获取策略执行所需行情 / 指标
3. 通过 `strategy_name` 找到对应工作区 skill
4. 自行读取该 skill 的 `SKILL.md`
5. 按策略定义执行筛选
6. 返回通过筛选的股票列表与理由

### 5.3 为什么不再让 orchestrator 传入 `symbols`

因为当前仅考虑 A 股市场，`symbols` 不再是一个需要上游显式传递的业务输入。  
如果仍然保留独立股票池阶段，只会增加：

- 一次中间 JSON 传递
- 一次无必要的阶段拆分
- 一个职责上并不独立的 subagent

因此在当前边界下，合并为单阶段更合理。

## 6. 策略 Skill 设计

### 6.1 技术决策

策略 skill 不使用 `nanobot/skills/` 目录中的内置 skill 体系承载，而是放在**当前工作区**的 `skills/` 目录。

例如：

- `skills/b1/SKILL.md`
- `skills/b2/SKILL.md`

### 6.2 读取方式

由 **screening 子 agent 自主决定是否读取**对应策略 skill。

这意味着：

- orchestrator 不预读取 skill
- orchestrator 不把 skill 正文直接塞给 screening
- screening 负责“取股票池 + 结合策略上下文自主选 skill + 做筛选”的完整过程

### 6.3 `strategy_name` 与 skill 的映射

当前公开策略名仅保留：

- `B1`
- `B2`

具体使用哪个 workspace skill 由 screening 子 agent 自主决定。

### 6.4 skill 的职责边界

策略 skill 应承载：

- 策略定义
- 筛选逻辑
- 指标解释
- 风险提示口径

策略 skill 不应承载：

- MCP 配置
- 市场范围
- orchestrator 编排逻辑
- 最终日报输出格式

## 7. Prompt 分层与模板设计

### 7.1 分层原则

每个阶段采用两层 prompt：

1. `system prompt`
   - 长期稳定规则
   - 角色身份
   - 职责边界
   - 输出硬约束
   - 失败处理口径

2. `user task prompt`
   - 本次运行的输入工单
   - 动态上下文
   - 输出 JSON 契约示例

### 7.2 单条 system 消息

运行时只发送一条 `system` 消息。  
其内容由以下两部分在执行前合并：

- 通用 subagent system prompt
- 当前阶段的 system overlay

这样做的原因是：

- 跨 provider 行为更稳定
- 系统规则更集中
- 减少多条 `system` 消息引入的歧义

### 7.3 screening prompt 的特别要求

`stock-screening` 的 system prompt 必须明确：

- 从 MCP 获取 A 股全量股票池
- 自行读取工作区策略 skill
- 输出 JSON-only

`stock-screening` 的 task prompt 不再包含：

- `symbols`

它只应包含：

- `trade_date`
- `strategy_name`
- 输出契约示例

## 8. 数据契约设计

### 8.1 中间结构

新的流程下，中间结构应重点围绕：

- `ScreeningResult` 或等价“筛选通过结果”
- `NewsFilteredStock`
- `ScoredStock`

原来的 `StockUniverseResponse` 在目标方案中不再作为独立阶段对外暴露结构。

### 8.2 最终结构

最终仍保留 `DailySelectionReport`：

- `trade_date`
- `strategy_name`
- `market`
- `selected_stocks`
- `summary`
- `global_risk_disclaimer`
- `partial_failures`

## 9. 错误处理与降级策略

### 9.1 原则

- 不静默失败
- 不编造数据
- 能降级就降级
- 降级必须可见

### 9.2 当前建议规则

- 不因缺少某个固定策略 skill 路径而在 orchestrator 层直接报错
- 策略 skill 文件为空或格式错误：直接报错
- MCP 无法返回 A 股股票池：直接报错
- 新闻工具不可用：允许保留标的，但写入 `partial_failures`
- 评分阶段缺失股票结果：该股票不进入最终 `selected_stocks`
- 任一阶段返回非法 JSON：工作流失败并返回明确错误

## 10. 触发与运行方式

### 10.1 当前触发方式

当前仍通过命令触发：

```text
/stock-report <strategy> [YYYY-MM-DD]
```

### 10.2 后续扩展

后续可接入：

- `CronService` 定时盘后日报
- API 触发入口
- 渠道自动投递

## 11. 当前实现状态

本节必须区分“目标设计”与“已完成实现”。

### 11.1 已完成

- `/stock-report` 命令入口
- orchestrator 基础骨架
- `run_inline()` 前台 subagent 执行方式
- 阶段化 prompt 体系
- prompt 文件化
- 单 system message 组合方式
- `AgentLoop` 默认挂载 `stock_selection_orchestrator`

### 11.2 尚未完成

- 真实 MCP 行情接入
- screening 自读工作区策略 skill
- 四阶段重构与当前代码完全同步
- Cron 定时日报
- 显式 HTTP stock-report endpoint
- 在当前工作环境中完成全量自动化验证

### 11.3 当前残留问题

当前部分代码或文档中，可能仍保留旧版五阶段架构痕迹，例如：

- 独立 `stock-universe` 阶段
- `stock-screening` 接收 `symbols`
- 五阶段 prompt / test 描述

这些都应被视为待重构内容，而不是最终设计。

## 12. 后续实施建议

### 12.1 第一优先级

完成 `stock-screening` 阶段重构：

- 接入 A 股股票池 MCP 能力
- 接入行情 / 技术指标能力
- 实现工作区策略 skill 定位与读取
- 只输出通过筛选的股票

### 12.2 第二优先级

接通风险与评分能力：

- `news-filter` 接新闻工具 / skill
- `market-scoring` 接技术评分所需数据

### 12.3 第三优先级

增加调度与交付：

- 盘后自动日报
- WebUI 报告可视化
- 渠道推送

## 13. 风险与技术决策

### 13.1 关键技术决策

| 决策 | 选择 | 原因 |
|---|---|---|
| 市场范围 | 固定 A 股 | 第一版收敛复杂度 |
| 阶段数量 | 四阶段 | 降低无意义中间传递 |
| 策略载体 | 工作区 `skills/` | 策略与编排解耦 |
| 策略读取位置 | screening 自读 | orchestrator 更轻，职责更清楚 |
| 中间输出格式 | JSON-only | 便于稳定编排与验证 |

### 13.2 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| MCP 数据不稳定 | screening 无法完成 | 明确报错或做降级设计 |
| skill 内容质量不稳定 | 策略执行漂移 | 用显式映射与规范化模板约束 |
| 五阶段旧痕迹残留 | 文档与代码不一致 | 按本方案统一重构 |
| 新闻能力不稳定 | 风险过滤质量下降 | 记录 `partial_failures` 并显式暴露 |

## 14. 总结

本方案将 A 股选股工作流收敛为一条更符合当前业务边界的四阶段流水线：

1. `stock-screening`
2. `news-filter`
3. `market-scoring`
4. `report-summary`

其中最关键的变化是：

- 将 `stock-universe` 与 `stock-screening` 合并
- 由 screening 阶段直接从 MCP 获取 A 股全量股票池
- 由 screening 阶段自行读取工作区 `skills/` 中的策略 skill

这使得 orchestrator 更轻，阶段职责更清楚，也更适合作为后续真实行情接入与策略扩展的基础架构。
