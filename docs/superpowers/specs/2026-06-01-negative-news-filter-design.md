# 负面新闻过滤器设计

## 文档信息

- 作者：Codex
- 日期：2026-06-01
- 状态：草案
- 范围：集成到现有股票子代理工作流中的 `nanobot-stock` A 股负面新闻过滤器

## 1. 目标

为 `nanobot-stock` 设计并集成一个批量负面新闻过滤器，在候选 A 股进入市场打分之前，先根据近期重大负面新闻进行筛选。

该过滤器必须：

- 接收来自 `stock-screening` 的候选股票代码列表
- 评估最近 7 天内的 A 股相关新闻
- 排除存在已确认重大负面新闻的股票代码
- 允许通过的股票继续进入 `market-scoring`
- 对外最终返回通过筛选的股票列表

该功能实现于 `nanobot-stock`，而不是 `TradingAgents-Astock`。

## 2. 设计原则

- 保持现有四阶段工作流不变：
  - `stock-screening`
  - `news-filter`
  - `market-scoring`
  - `report-summary`
- 复用 `TradingAgents-Astock` 的 A 股新闻源思路，但不采用其长篇 `news_analyst` 报告模式
- 使用混合式设计：规则预筛 + 子代理复核
- 保持对外业务输出简洁：只返回通过的股票代码
- 保留内部证据，便于调试、审计和后续报告扩展

## 3. 非目标

- 不生成完整投研报告
- 不直接复用 `policy`、`fundamentals` 或 `lockup` 分析代理
- 不自动下单
- 不做广义市场情绪预测
- v1 不引入机器学习分类器

## 4. 集成位置

该功能集成到 `nanobot-stock` 现有的 `news-filter` 阶段中。

它不会新增第五个阶段。

### 现有流程

```text
stock-screening
-> news-filter
-> market-scoring
-> report-summary
```

### 升级后的 `news-filter` 阶段

```text
candidate symbols
-> A-share news adapter
-> rule prescreen
-> subagent review
-> allowed symbols
```

## 5. 高层架构

整体设计拆分为四层。

### 5.1 新闻适配层

按股票代码抓取最近 7 天内的 A 股相关新闻。

### 5.2 规则预筛层

使用低成本、高召回的启发式规则识别可疑负面新闻候选。

### 5.3 子代理复核层

仅复核命中预筛规则的内容，并给出最终风险判断。

### 5.4 编排层

协调阶段执行、校验 JSON 契约，并仅将允许通过的股票代码送入 `market-scoring`。

## 6. 业务契约

### 6.1 输入

- 来自 `stock-screening` 的候选股票代码
- 固定回看窗口：7 天

### 6.2 对外输出

对外可见的业务输出是通过筛选的股票代码列表。

```json
{
  "passed_symbols": ["600001", "000001", "300750"]
}
```

### 6.3 内部阶段输出

内部的 `news-filter` 阶段会保留更丰富的数据结构，以便兼容现有流程并支持调试。

```json
{
  "items": [
    {
      "symbol": "600001",
      "allowed": true,
      "decision": "PASS",
      "matched_categories": [],
      "negative_news_flags": [],
      "risk_notes": [],
      "evidence": []
    }
  ],
  "partial_failures": []
}
```

编排器仍然以 `allowed` 作为控制流程的核心字段。

## 7. 负面事件范围

v1 支持以下十类重大负面新闻：

1. 业绩暴雷
2. 监管调查或行政处罚
3. 重大减持计划或限售解禁压力
4. 债务或诉讼风险
5. 退市或 ST 风险
6. 重大事故或停产停工
7. 核心高管异常变动
8. 股权质押或平仓风险
9. 重大合同或订单失效
10. 非标准审计意见

## 8. 新闻适配器设计

### 8.1 职责

适配器使用与 `TradingAgents-Astock` 一致的新闻源思路抓取近期 A 股新闻。

它应将输出统一为单一的内部文章结构，并避免在此层嵌入投资判断。

### 8.2 输入

- `symbol`
- `lookback_days=7`

### 8.3 输出结构

```json
[
  {
    "title": "公司收到证监会立案告知书",
    "summary": "公司披露涉嫌信息披露违法违规……",
    "published_at": "2026-06-01T09:30:00+08:00",
    "source": "Eastmoney"
  }
]
```

### 8.4 要求

- 统一时间戳格式
- 优先保留同时包含 `title` 和 `summary` 的文章对象
- 对明显重复的内容去重
- 暴露依赖失败信息，以便纳入 `partial_failures`

## 9. 规则预筛设计

### 9.1 目标

规则层不直接做最终排除判断。

它只负责以低成本、高召回的方式筛出候选项，从而实现：

- 明显安全的股票可以快速通过
- 可疑股票会升级到子代理复核

### 9.2 处理步骤

1. 规范化 `title + summary` 文本
2. 匹配类别关键词词典
3. 应用上下文排除规则
4. 输出候选文章及粗粒度严重级别

### 9.3 输出结构

```json
{
  "symbol": "600001",
  "has_negative_candidates": true,
  "candidate_articles": [
    {
      "date": "2026-06-01",
      "title": "公司收到证监会立案告知书",
      "matched_keywords": ["立案", "证监会"],
      "candidate_categories": ["regulatory investigation or administrative penalty"],
      "rule_severity": "high"
    }
  ]
}
```

### 9.4 预筛决策策略

规则层应当：

- 未发现候选文章时，直接放行股票代码
- 一旦存在候选文章，就升级到子代理复核

在 v1 中，规则层不能直接拒绝股票。

## 10. 上下文排除规则

### 10.1 目的

上下文排除规则用于在关键词命中后进一步降低误报。

它作为第二道检查，会寻找相邻上下文中的证据，判断命中的关键词是否真的代表重大负面新闻。

### 10.2 规则模型

每一类负面事件都应支持：

- `include` 词项
- `exclude` 词项
- 粗粒度严重级别

概念示例如下：

```python
NEGATIVE_RULES = {
    "regulatory_investigation": {
        "include": ["立案", "调查", "处罚", "监管函", "警示函", "证监会"],
        "exclude": ["机构调研", "投资者调研", "调研纪要"],
        "severity": "high",
    }
}
```

### 10.3 优先级策略

规则层应区分强触发词和弱触发词。

#### 强触发词

示例：

- `立案`
- `处罚`
- `违约`
- `冻结`
- `退市`
- `爆炸`
- `停产`

处理方式：

- 不能因为弱排除证据而直接丢弃
- 只能降级或保留给子代理复核

#### 弱触发词

示例：

- `风险`
- `调查`
- `调整`
- `辞职`
- `减持`

处理方式：

- 如果排除上下文明确存在，则丢弃
- 如果语义混杂或存在歧义，则降级但仍升级复核

### 10.4 输出状态

上下文检查应输出以下三种状态之一：

- `keep`
- `downgrade`
- `drop`

### 10.5 重点误报场景

v1 应显式处理以下高频误报：

- `调查` 与 `机构调研` / `投资者调研`
- `风险` 与通用 `风险提示公告`
- `辞职` 与正常的 `任期届满` / `换届`
- `诉讼` 与 `已结案` / `胜诉`
- `减持` 与 ETF 或基金调仓 / 被动卖出

## 11. 子代理复核设计

### 11.1 职责

子代理只复核规则层输出的候选文章。

它负责判断该股票应当被标记为：

- `PASS`
- `REVIEW`
- `REJECT`

### 11.2 到 `allowed` 的映射

- `PASS` -> `allowed: true`
- `REVIEW` -> v1 中仍为 `allowed: true`，但附加风险说明
- `REJECT` -> `allowed: false`

### 11.3 输出结构

```json
{
  "symbol": "600001",
  "allowed": false,
  "decision": "REJECT",
  "matched_categories": ["regulatory investigation or administrative penalty"],
  "negative_news_flags": ["CSRC investigation"],
  "risk_notes": ["recent material negative news within 7 days"],
  "evidence": [
    {
      "date": "2026-06-01",
      "title": "Company receives CSRC investigation notice",
      "category": "regulatory investigation or administrative penalty",
      "severity": "high"
    }
  ]
}
```

### 11.4 Prompt 约束

`news-filter` 子代理的 prompt 必须：

- 只输出合法 JSON
- 不输出 markdown 或额外说明文字
- 不得编造缺失新闻
- 只允许复核提供的候选文章
- 除非阶段设计明确允许，否则不得抓取或推断证据集之外的无关事实

## 12. 编排器行为

### 12.1 职责

编排器必须：

- 接收筛选后的股票代码
- 调用升级后的 `news-filter` 阶段
- 解析并校验返回的 JSON
- 仅保留 `allowed` 的股票代码
- 将这些股票继续传入 `market-scoring`

### 12.2 兼容性要求

编排器应继续将 `allowed` 作为最低限度的硬性契约字段。

其他字段例如：

- `decision`
- `matched_categories`
- `evidence`

可以被接受并保留，但不要求参与控制流。

## 13. 文件级落点

该设计预计落在 `nanobot-stock` 的以下位置：

- `nanobot/stocks/news_adapter.py`
- `nanobot/stocks/news_rules.py`
- `nanobot/templates/stocks/system/news_filter.md`
- `nanobot/templates/stocks/tasks/news_filter.md`
- `nanobot/stocks/orchestrator.py`
- `tests/stocks/test_subagent_orchestrator.py`

具体文件名可以略有调整，但职责边界应保持一致。

## 14. 失败处理

### 14.1 新闻源失败

- 不得伪造负面证据
- 记录一条 `partial_failures`
- 在 v1 中，除非已确认存在负面新闻，否则默认保留该股票代码

### 14.2 规则层失败

- 优先按单个股票降级处理，而不是让整批失败
- 在诊断信息中暴露问题

### 14.3 子代理 JSON 非法

- 视为 `news-filter` 阶段失败
- 由编排器抛出明确错误

### 14.4 单股票失败

- 尽可能将失败隔离在该股票上
- 避免拖垮整批处理

## 15. 测试要求

### 15.1 单元测试

新增以下测试：

- 类别关键词匹配
- 上下文排除规则
- 候选文章生成
- `PASS/REVIEW/REJECT` 到 `allowed` 的决策映射

### 15.2 编排器测试

扩展 `tests/stocks/test_subagent_orchestrator.py`，验证：

- 扩展后的 `news-filter` JSON 字段能够被接受
- `allowed=false` 的股票绝不会进入 `market-scoring`
- 最终通过的股票列表输出正确

### 15.3 失败测试

覆盖以下场景：

- 新闻源依赖失败
- 新闻集合为空
- 子代理 JSON 非法
- 所有股票都被拒绝
- 所有股票都通过

## 16. 实施阶段

### 阶段 1

- 引入 A 股新闻适配器
- 实现负面新闻规则词典
- 实现预筛层
- 为规则层补齐测试

### 阶段 2

- 升级 `news_filter` 的 system 和 task 模板
- 将预筛输出接入子代理复核
- 扩展内部 JSON 契约

### 阶段 3

- 更新编排器的合并行为
- 对外暴露通过筛选的股票列表
- 完成集成测试

## 17. 最终总结

该设计将 `nanobot-stock` 现有的 `news-filter` 阶段升级为一个面向生产的 A 股负面新闻过滤器。

系统复用了 `TradingAgents-Astock` 的 A 股新闻源思路，但不沿用其分析师式报告流程。

核心模型如下：

- 规则负责高召回候选检测
- 子代理负责语义复核与最终裁决
- 编排器只保留允许通过的股票
- 面向业务的输出只返回最终通过的股票列表

一句话概括：

**规则负责召回，子代理负责裁决，编排器负责路由，对外接口只返回通过的股票代码。**
