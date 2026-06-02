# Negative News Filter Design

## Document Info

- Author: Codex
- Date: 2026-06-01
- Status: Draft
- Scope: `nanobot-stock` A-share negative news filter integrated into the existing stock subagent workflow

## 1. Objective

Design and integrate a batch negative-news filter into `nanobot-stock` so that A-share candidate symbols are filtered by recent material negative news before they proceed to market scoring.

The filter must:

- Consume a list of candidate stock symbols from `stock-screening`
- Evaluate recent A-share news within the last 7 days
- Exclude symbols with confirmed material negative news
- Allow passing symbols to continue into `market-scoring`
- Return the final externally visible result as the passed stock list

This feature is implemented in `nanobot-stock`, not in `TradingAgents-Astock`.

## 2. Design Principles

- Preserve the existing four-stage workflow:
  - `stock-screening`
  - `news-filter`
  - `market-scoring`
  - `report-summary`
- Reuse the A-share news-source approach from `TradingAgents-Astock`, but not its long-form `news_analyst` report pattern
- Use a hybrid design: rule-based prescreening plus subagent review
- Keep the external business output narrow: return only the symbols that passed
- Preserve internal evidence for debugging, auditability, and future reporting

## 3. Non-Goals

- No full investment research report generation
- No direct reuse of `policy`, `fundamentals`, or `lockup` analysts
- No automatic order placement
- No broad market sentiment prediction
- No machine-learned classifier in v1

## 4. Integration Point

The feature is integrated into the existing `news-filter` stage of `nanobot-stock`.

It does not add a fifth stage.

### Existing flow

```text
stock-screening
-> news-filter
-> market-scoring
-> report-summary
```

### Upgraded `news-filter` stage

```text
candidate symbols
-> A-share news adapter
-> rule prescreen
-> subagent review
-> allowed symbols
```

## 5. High-Level Architecture

The design is split into four layers.

### 5.1 News Adapter Layer

Fetch recent A-share news for each symbol over the last 7 days.

### 5.2 Rule Prescreen Layer

Identify suspicious negative-news candidates with cheap, high-recall heuristics.

### 5.3 Subagent Review Layer

Review only prescreen hits and produce the final risk decision.

### 5.4 Orchestration Layer

Coordinate stage execution, validate JSON contracts, and move only allowed symbols to `market-scoring`.

## 6. Business Contract

### 6.1 Inputs

- Candidate stock symbols from `stock-screening`
- Fixed lookback window: 7 days

### 6.2 External Output

The externally visible business output is the list of passed symbols.

```json
{
  "passed_symbols": ["600001", "000001", "300750"]
}
```

### 6.3 Internal Stage Output

The internal `news-filter` stage keeps a richer structure for compatibility and debugging.

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

The orchestrator continues to rely on `allowed` as the controlling field.

## 7. Negative Event Scope

Version 1 supports these ten material negative-news categories:

1. Earnings blow-up
2. Regulatory investigation or administrative penalty
3. Major reduction plan or lockup-expiry pressure
4. Debt or litigation risk
5. Delisting or ST risk
6. Major accident or production shutdown
7. Abnormal core executive change
8. Equity pledge or liquidation risk
9. Major contract or order failure
10. Non-standard audit opinion

## 8. News Adapter Design

### 8.1 Responsibility

The adapter fetches recent A-share news using a source approach aligned with `TradingAgents-Astock`.

It should unify output into a single internal article shape and avoid embedding investment judgment.

### 8.2 Input

- `symbol`
- `lookback_days=7`

### 8.3 Output Shape

```json
[
  {
    "title": "Company receives CSRC investigation notice",
    "summary": "The company disclosed suspected information disclosure violations...",
    "published_at": "2026-06-01T09:30:00+08:00",
    "source": "Eastmoney"
  }
]
```

### 8.4 Requirements

- Normalize timestamps
- Prefer article objects with both `title` and `summary`
- Deduplicate obvious duplicates
- Surface dependency failures so they can become `partial_failures`

## 9. Rule Prescreen Design

### 9.1 Goal

The rule layer does not make the final exclusion decision.

It only performs low-cost, high-recall candidate detection so that:

- obviously safe symbols can pass quickly
- suspicious symbols are escalated to the subagent

### 9.2 Processing Steps

1. Normalize text from `title + summary`
2. Match category keyword dictionaries
3. Apply contextual exclusion rules
4. Emit candidate articles and coarse severity

### 9.3 Output Shape

```json
{
  "symbol": "600001",
  "has_negative_candidates": true,
  "candidate_articles": [
    {
      "date": "2026-06-01",
      "title": "Company receives CSRC investigation notice",
      "matched_keywords": ["立案", "证监会"],
      "candidate_categories": ["regulatory investigation or administrative penalty"],
      "rule_severity": "high"
    }
  ]
}
```

### 9.4 Prescreen Decision Policy

The rule layer should:

- pass symbols directly if no candidate articles are found
- escalate symbols to subagent review if candidate articles exist

It must not directly reject symbols in v1.

## 10. Contextual Exclusion Rules

### 10.1 Purpose

Contextual exclusion rules reduce false positives after keyword hits.

They operate as a second pass that looks for nearby evidence that a matched keyword is not actually signaling material negative news.

### 10.2 Rule Model

Each negative category should support:

- `include` terms
- `exclude` terms
- coarse severity

Example conceptual structure:

```python
NEGATIVE_RULES = {
    "regulatory_investigation": {
        "include": ["立案", "调查", "处罚", "监管函", "警示函", "证监会"],
        "exclude": ["机构调研", "投资者调研", "调研纪要"],
        "severity": "high",
    }
}
```

### 10.3 Priority Policy

The rule layer should distinguish between strong and weak triggers.

#### Strong triggers

Examples:

- `立案`
- `处罚`
- `违约`
- `冻结`
- `退市`
- `爆炸`
- `停产`

Behavior:

- never drop on weak exclusion evidence
- downgrade or keep for subagent review

#### Weak triggers

Examples:

- `风险`
- `调查`
- `调整`
- `辞职`
- `减持`

Behavior:

- if exclusion context is clearly present, drop
- if mixed or ambiguous, downgrade and still escalate

### 10.4 Output States

Context checks should result in one of:

- `keep`
- `downgrade`
- `drop`

### 10.5 Priority False-Positive Cases

Version 1 should explicitly address these high-frequency false positives:

- `调查` vs `机构调研` / `投资者调研`
- `风险` vs generic `风险提示公告`
- `辞职` vs normal `任期届满` / `换届`
- `诉讼` vs `已结案` / `胜诉`
- `减持` vs ETF or fund rebalancing / passive selling

## 11. Subagent Review Design

### 11.1 Responsibility

The subagent reviews only the candidate articles produced by the rule layer.

It decides whether the symbol should be:

- `PASS`
- `REVIEW`
- `REJECT`

### 11.2 Mapping to `allowed`

- `PASS` -> `allowed: true`
- `REVIEW` -> `allowed: true` in v1, plus risk notes
- `REJECT` -> `allowed: false`

### 11.3 Output Shape

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

### 11.4 Prompt Constraints

The `news-filter` subagent prompt must:

- output valid JSON only
- avoid markdown or prose
- avoid fabricating missing news
- only review the supplied candidate articles
- not fetch or infer unrelated facts beyond the provided evidence set unless explicitly allowed by the stage design

## 12. Orchestrator Behavior

### 12.1 Responsibilities

The orchestrator must:

- receive screened symbols
- invoke the upgraded `news-filter` stage
- parse and validate returned JSON
- retain only `allowed` symbols
- pass those symbols into `market-scoring`

### 12.2 Compatibility Requirement

The orchestrator should continue to use `allowed` as the minimum hard contract.

Additional fields such as:

- `decision`
- `matched_categories`
- `evidence`

may be accepted and preserved without being required for control flow.

## 13. File-Level Placement

The design is intended to land in these areas of `nanobot-stock`:

- `nanobot/stocks/news_adapter.py`
- `nanobot/stocks/news_rules.py`
- `nanobot/templates/stocks/system/news_filter.md`
- `nanobot/templates/stocks/tasks/news_filter.md`
- `nanobot/stocks/orchestrator.py`
- `tests/stocks/test_subagent_orchestrator.py`

The exact filenames can vary slightly, but the boundaries should remain the same.

## 14. Failure Handling

### 14.1 News Source Failure

- do not fabricate negative evidence
- record a `partial_failures` entry
- default to preserving the symbol in v1 unless confirmed negative news exists

### 14.2 Rule Layer Failure

- prefer per-symbol degradation over batch failure
- surface the issue in diagnostics

### 14.3 Invalid Subagent JSON

- treat as `news-filter` stage failure
- raise a clear orchestrator error

### 14.4 Per-Symbol Failure

- isolate failure to that symbol where possible
- avoid collapsing the entire batch

## 15. Testing Requirements

### 15.1 Unit Tests

Add tests for:

- category keyword matches
- contextual exclusion rules
- candidate article generation
- decision mapping from `PASS/REVIEW/REJECT` to `allowed`

### 15.2 Orchestrator Tests

Extend `tests/stocks/test_subagent_orchestrator.py` to verify:

- expanded `news-filter` JSON fields are accepted
- `allowed=false` symbols never reach `market-scoring`
- final passed symbol output is correct

### 15.3 Failure Tests

Cover:

- news-source dependency failure
- empty news set
- invalid subagent JSON
- all symbols rejected
- all symbols passed

## 16. Implementation Phases

### Phase 1

- introduce the A-share news adapter
- implement negative-news rule dictionaries
- implement the prescreen layer
- cover the rule layer with tests

### Phase 2

- upgrade `news_filter` system and task templates
- connect the prescreen output to subagent review
- extend the internal JSON contract

### Phase 3

- update orchestrator merge behavior
- expose the externally visible passed-symbol list
- complete integration tests

## 17. Final Summary

This design upgrades the existing `news-filter` stage in `nanobot-stock` into a production-oriented A-share negative-news filter.

The system reuses the A-share news-source approach from `TradingAgents-Astock`, but not its analyst-style reporting flow.

The core model is:

- rules perform high-recall candidate detection
- the subagent performs semantic review and final judgment
- the orchestrator preserves only allowed symbols
- the business-facing output returns the final passed stock list

In short:

**rules recall, subagent adjudicates, orchestrator routes, and the external interface returns only the symbols that passed.**
