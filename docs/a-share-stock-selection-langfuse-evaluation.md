# A 股选股流程的 Langfuse 分阶段 Judge 指南

## 1. 适用范围

本文说明如何对当前 A 股选股主流程使用 Langfuse 的 LLM-as-a-judge 能力做分阶段评测。目标不是评判“股票是否真的会涨”，而是评判当前工作流在每个阶段的输出是否：

- 符合约定的 contract
- 与输入和上下游阶段保持自洽
- 不引入无依据的扩展解释或幻觉
- 在失败或降级场景下保持可控行为

本文基于当前 `StockSelectionSubagentOrchestrator` 的真实主链路编写，适用于以下已启用阶段：

1. `stock-screening`
2. `prepare-market-scoring-inputs`
3. `market-scoring`
4. `merge-stage-outputs`
5. `run_daily_stock_selection`

当前代码中虽然仍保留 `news-filter` 与 `report-summary` 的相关逻辑，但它们不在主执行链路中：

- `news-filter` 当前在主流程中被注释掉
- `report-summary` 当前在主流程中被注释掉

因此本文将它们视为后续扩展阶段，不纳入当前最小评测实施集。

## 2. Judge 设计原则

### 2.1 先做规则校验，再做 LLM judge

并不是所有问题都适合交给 LLM 来判断。对于可以通过代码直接验证的内容，应优先使用 deterministic checks，例如：

- JSON 是否可解析
- 必填字段是否存在
- `passed_count` 是否等于 `items` 长度
- `technical_score` 是否落在 `0-100`
- 排序与截断是否正确

LLM-as-a-judge 更适合评估：

- 语义是否自洽
- 文本理由是否与分数一致
- 是否越权引入外部知识
- 是否输出了不应出现的扩展结论

### 2.2 Judge 只看输入与输出，不看真实市场

Judge 的目标是评估工作流质量，不是预测未来收益。因此 prompt 中必须明确：

- 只根据提供的 `input` 与 `output` 判断
- 不使用外部市场知识
- 不根据真实走势、板块表现、基本面消息进行加减分

### 2.3 分阶段比全链路单点评分更有价值

如果只评最终 `run_daily_stock_selection` 输出，很多问题会被掩盖。例如：

- `stock-screening` contract 偏移
- `market-scoring` 单股结果和输入 symbol 不一致
- 技术数据准备阶段降级不当

分阶段 judge 的价值在于：

- 更容易定位问题来自哪个 observation
- 更适合和 Langfuse observation-level evaluator 结合
- 更适合后续扩展到 `news-filter` 等阶段

### 2.4 评分维度要窄、明确、结构化

每个 evaluator 应只评少量明确维度，不要把所有质量问题压到一个总分里。推荐 judge 返回结构化结果，例如：

- `verdict`
- `score`
- `reasoning`
- `issues`

同时建议将每个核心维度拆成单独的 score name，便于在 Langfuse 中趋势化观察。

## 3. 分阶段评测方案

### 3.1 阶段一：`stock-screening`

#### 当前职责

该阶段负责执行策略筛选，并返回通过筛选的候选标的。当前 contract 的重点是：

- 顶层包含 `items`
- 每个 item 至少包含 `symbol` 和 `strategy_name`
- 顶层可包含 `screened_count`、`passed_count`、`screening_notes`

当前该阶段不再依赖以下字段：

- `screen_pass_reasons`
- `risk_notes`

这些字段不应作为 judge 的必检 contract。

#### 要测的内容

1. 结构正确性
- 输出是否为合法 JSON
- 顶层字段是否存在且类型正确
- `items` 是否为数组
- item 是否至少包含 `symbol`、`strategy_name`

2. 计数一致性
- `passed_count` 是否等于 `items` 长度
- `screened_count` 是否大于等于 `passed_count`
- 计数字段是否为合理整数

3. 结果边界
- `items` 中是否只包含通过筛选的标的
- 是否错误输出了 failed list
- 是否混入废弃字段依赖

4. 输入一致性
- item 中的 `strategy_name` 是否与请求策略一致

#### 如何测试

优先做 deterministic checks：

- JSON schema 校验
- `passed_count == len(items)`
- `screened_count >= passed_count`
- 每个 item 的必要字段检查

然后可补一个轻量 LLM judge，专门判断：

- 输出是否像一个“筛选阶段结果”
- 是否只返回通过标的
- 是否存在明显的 contract 偏离

#### 是否推荐 LLM judge

推荐，但应作为辅助校验，不应替代规则校验。

#### 推荐 observation target / score names

- Observation target: `stock-screening`
- 推荐 score names:
  - `screening_contract_ok`
  - `screening_count_consistency`
  - `screening_semantic_ok`

### 3.2 阶段二：`prepare-market-scoring-inputs`

#### 当前职责

该阶段负责把筛选结果转换成市场评分阶段的输入，并为每个 symbol 生成对应的 `technical_snapshot`。在技术数据不可用或批量失败时，需要构造可继续向下游传递的 fallback 结构。

#### 要测的内容

1. 输入覆盖率
- 上游的每个 symbol 是否都被保留
- 是否存在 symbol 丢失
- 是否出现额外无关 symbol

2. snapshot 结构完整性
- 每个 item 是否都有 `symbol`
- 每个 item 是否都有 `technical_snapshot`
- fallback 输出是否携带 `data_status` / `reason`

3. 降级与失败行为
- 批量技术数据失败时，是否仍然为每个 symbol 生成可评分的 item
- `partial_failures` 是否准确记录

4. 数据变换正确性
- flatten 之后的 snapshot 是否保留评分所需字段
- wrapper 层的状态字段是否正确下沉

#### 如何测试

这一阶段应以 deterministic checks 为主：

- 输入 symbol 集合与输出 symbol 集合比较
- 必填字段存在性检查
- fallback 路径和 `partial_failures` 内容检查
- 批量异常场景与缺失单 symbol 场景的测试

如果需要 LLM judge，也只建议用于评估：

- snapshot 文本是否显著缺失关键上下文
- fallback 文案是否清楚表达了数据不可用

但这不是优先项。

#### 是否推荐 LLM judge

不推荐作为主方案。优先使用 deterministic checks。

#### 推荐 observation target / score names

- Observation target: `prepare-market-scoring-inputs`
- 推荐 score names:
  - `market_input_coverage_ok`
  - `market_input_shape_ok`
  - `market_input_fallback_ok`

### 3.3 阶段三：`market-scoring`

#### 当前职责

该阶段对单只股票执行技术评分。当前每只股票是单独调用一次评分阶段，因此可以天然映射到 observation 级别的 judge。

输出重点字段包括：

- `symbol`
- `technical_score`
- `score_reasons`
- `risk_notes`

#### 要测的内容

1. 单项 contract
- 输出是否只包含一个 item
- 输出中的 `symbol` 是否与请求 symbol 一致
- `technical_score` 是否为 `0-100` 范围内的整数

2. 理由与分数自洽
- 高分时，`score_reasons` 不应明显与分数冲突
- 低分时，`risk_notes` 不应为空且说明应合理
- `score_reasons` 与 `risk_notes` 不应互相矛盾

3. 基于输入而非幻觉
- 不应提到 snapshot 中不存在的技术指标
- 不应扩展到新闻、基本面、行业、宏观等外部知识
- 不应输出与输入 symbol 无关的泛化市场叙述

4. unavailable 场景
- 技术数据不可用时，是否合理输出低分或零分
- `risk_notes` 是否说明数据不可用或不足

#### 如何测试

推荐先做 deterministic checks：

- `len(items) == 1`
- `item.symbol == requested_symbol`
- `technical_score` 为整数
- `0 <= technical_score <= 100`

再做重点 LLM judge：

- 评估理由与分数是否一致
- 评估是否存在技术指标幻觉
- 评估是否越界使用外部知识

该阶段是当前最适合重点投入 LLM-as-a-judge 的阶段。

#### 是否推荐 LLM judge

强烈推荐。该阶段是当前流程中最有价值的 LLM judge 切入点。

#### 推荐 observation target / score names

- Observation target: `market-scoring:{symbol}`
- 推荐 score names:
  - `market_score_contract_ok`
  - `market_score_symbol_match`
  - `market_score_reasoning_quality`
  - `market_score_groundedness`

### 3.4 阶段四：`merge-stage-outputs`

#### 当前职责

该阶段负责将评分结果映射为最终 `SelectedStockReport` 列表，并执行：

- 按 `technical_score` 降序排序
- 最多保留前 10 只
- 将评分字段映射到最终结构

#### 要测的内容

1. 排序正确性
- 是否按 `technical_score` 降序排序
- 同分时的排序行为是否稳定且符合当前实现

2. top 10 截断正确性
- 输出是否最多保留 10 条

3. 字段映射正确性
- `technical_score`、`score_reasons`、`risk_notes` 是否正确保留
- 当前固定为空的字段是否保持空值约定

#### 如何测试

这一阶段应使用 deterministic checks：

- 构造 12 个以上评分结果，验证 top 10 排序
- 构造同分场景，检查 tie-break 行为
- 验证字段映射无丢失

#### 是否推荐 LLM judge

不推荐。该阶段是纯程序逻辑，规则校验更可靠。

#### 推荐 observation target / score names

- Observation target: `merge-stage-outputs`
- 推荐 score names:
  - `merge_sort_ok`
  - `merge_top10_ok`
  - `merge_field_mapping_ok`

### 3.5 阶段五：`run_daily_stock_selection`

#### 当前职责

这是最终对外结果阶段，负责产出 `DailySelectionReport`。当前重点字段包括：

- `trade_date`
- `strategy_name`
- `market`
- `selected_stocks`
- `summary`
- `global_risk_disclaimer`
- `partial_failures`

#### 要测的内容

1. 顶层 contract
- 必填字段是否齐全
- 字段类型是否正确
- `selected_stocks` 是否为数组

2. 汇总一致性
- `summary` 中提到的股票数量是否与 `selected_stocks` 长度一致
- `trade_date`、`strategy_name`、`market` 是否前后一致

3. 范围与安全边界
- disclaimer 是否存在
- 输出是否越界成投资建议
- 是否出现与阶段结果无关的扩展结论

4. 最终结果整体质量
- 是否简洁
- 是否没有混入阶段内部 contract 细节
- 是否无明显自相矛盾

#### 如何测试

推荐先做 deterministic checks：

- 顶层 schema 校验
- `summary` 与 `selected_stocks` 数量一致性检查
- disclaimer 存在性检查

然后做最终 LLM judge：

- 评估最终报告的整体自洽性
- 评估是否偏离既定输出范围
- 评估是否引入不应出现的扩展解释

#### 是否推荐 LLM judge

推荐。该阶段适合作为最终对外质量闸门。

#### 推荐 observation target / score names

- Observation target: `run_daily_stock_selection`
- 推荐 score names:
  - `final_report_contract_ok`
  - `final_report_consistency`
  - `final_report_scope_ok`

## 4. 当前未启用阶段

### 4.1 `news-filter`

当前主流程中该阶段仍保留系统 prompt、task prompt 和子方法，但在主执行路径中处于注释状态，因此不应纳入当前最小评测集。

后续恢复时，可重点评估：

- `allowed` 路由是否正确
- `matched_categories`、`negative_news_flags`、`risk_notes` 是否自洽
- 是否只基于提供候选新闻判断，而不扩展外部新闻知识

### 4.2 `report-summary`

当前主流程中该阶段同样保留模板与方法，但不在主执行链路中。

后续恢复时，可重点评估：

- `summary` 是否与最终股票列表一致
- `global_risk_disclaimer` 是否稳定
- 是否没有发散到额外投资建议或未提供字段

## 5. 样本组织方式

为了让 evaluator 可重复、可对比，建议为每个阶段维护一组固定样本。推荐组织方式：

- `input.json`
- `output.json`
- `expected_labels.json`

推荐内容如下：

### `input.json`

记录该阶段收到的输入，例如：

- `stock-screening` 的策略名和交易日
- `market-scoring` 的 symbol 与 `technical_snapshot`
- `run_daily_stock_selection` 的上游组合结果

### `output.json`

记录阶段输出的原始 JSON，用于：

- deterministic checks
- LLM judge 输入

### `expected_labels.json`

记录期望标签，用于验证 evaluator 自身是否表现稳定。示例：

- `contract_ok`
- `count_consistency`
- `symbol_match`
- `groundedness`
- `score_band`

该文件不必一开始就很复杂，先覆盖最关键维度即可。

## 6. 推荐的 Langfuse evaluator 拆分

当前建议将 evaluator 拆为 3 组主 evaluator，而不是做一个覆盖全链路的超大 judge：

1. `screening_stage_judge`
- target: `stock-screening`
- 目标：contract、计数一致性、通过候选边界

2. `market_scoring_judge`
- target: `market-scoring:{symbol}`
- 目标：单股评分 contract、symbol 匹配、理由自洽、无幻觉

3. `final_report_judge`
- target: `run_daily_stock_selection`
- 目标：最终报告 contract、自洽性、summary/selected_stocks 一致、scope 边界

程序阶段：

- `prepare-market-scoring-inputs`
- `merge-stage-outputs`

优先使用 deterministic checks，而不是 LLM-as-a-judge。

## 7. 推荐最小落地顺序

为了尽快获得有价值结果，建议按以下顺序推进：

1. 先做 `market-scoring` 单股 judge
- 这是当前最值得用 LLM judge 的阶段
- 最容易发现输出质量与幻觉问题

2. 再做 `run_daily_stock_selection` 最终报告 judge
- 用于把关用户可见输出

3. 再补 `stock-screening` judge
- 主要约束 contract 与计数一致性

4. 最后补程序阶段 deterministic checks
- `prepare-market-scoring-inputs`
- `merge-stage-outputs`

## 8. 总结

对当前 A 股选股流程使用 Langfuse 做评测时，不应把“真实股票收益”当作 judge 目标，而应把重点放在：

- contract 是否正确
- 阶段输出是否自洽
- 是否严格基于输入
- 是否正确处理降级与边界

最实用的方案不是单一全链路总评，而是：

- 对程序阶段做 deterministic checks
- 对 LLM 输出密集阶段做 observation-level LLM judge
- 对最终结果做最终质量闸门

按此方案实施，可以在不引入过多评测复杂度的前提下，快速建立一套可持续迭代的 Langfuse 评测体系。
