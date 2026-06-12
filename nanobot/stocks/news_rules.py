"""Rule-based negative-news prescreening for A-share symbols."""

from __future__ import annotations

from dataclasses import dataclass

from nanobot.stocks.news_adapter import AdaptedNewsArticle


@dataclass(frozen=True)
class NegativeRule:
    category: str
    flag: str
    include: tuple[str, ...]
    severity: str


@dataclass(frozen=True)
class CandidateArticle:
    title: str
    summary: str
    source: str
    date: str
    matched_keywords: list[str]
    candidate_categories: list[str]
    rule_severity: str
    negative_news_flag: str


@dataclass(frozen=True)
class PrescreenResult:
    symbol: str
    has_negative_candidates: bool
    candidate_articles: list[CandidateArticle]


NEGATIVE_RULES: tuple[NegativeRule, ...] = (
    NegativeRule(
        category="regulatory investigation or administrative penalty",
        flag="CSRC investigation",
        include=("立案", "处罚", "警示函", "监管函", "证监会", "调查"),
        severity="high",
    ),
    NegativeRule(
        category="major reduction plan or lockup-expiry pressure",
        flag="major shareholder reduction plan",
        include=("减持计划", "拟减持", "清仓减持", "大额减持", "减持股份"),
        severity="high",
    ),
    NegativeRule(
        category="debt or litigation risk",
        flag="debt or litigation risk",
        include=("违约", "诉讼", "冻结", "被执行人", "债务逾期"),
        severity="high",
    ),
    NegativeRule(
        category="delisting or ST risk",
        flag="delisting or ST risk",
        include=("退市", "*st", "st风险", "终止上市"),
        severity="high",
    ),
    NegativeRule(
        category="major accident or production shutdown",
        flag="major accident or production shutdown",
        include=("爆炸", "事故", "停产", "停工"),
        severity="high",
    ),
    NegativeRule(
        category="abnormal core executive change",
        flag="abnormal executive resignation",
        include=("辞职", "离任", "协助调查"),
        severity="medium",
    ),
    NegativeRule(
        category="equity pledge or liquidation risk",
        flag="equity pledge liquidation risk",
        include=("质押", "平仓", "强制平仓"),
        severity="high",
    ),
    NegativeRule(
        category="major contract or order failure",
        flag="major contract failure",
        include=("订单取消", "合同终止", "订单流失"),
        severity="high",
    ),
    NegativeRule(
        category="non-standard audit opinion",
        flag="non-standard audit opinion",
        include=("保留意见", "无法表示意见", "否定意见", "非标意见"),
        severity="high",
    ),
    NegativeRule(
        category="earnings blow-up",
        flag="earnings blow-up",
        include=("业绩预亏", "业绩暴雷", "大幅下修", "巨额亏损"),
        severity="high",
    ),
)


def prescreen_negative_news(symbol: str, articles: list[AdaptedNewsArticle]) -> PrescreenResult:
    """Find potentially material negative-news articles for one symbol."""

    candidates: list[CandidateArticle] = []

    for article in articles:
        text = _normalize_text(article.text)
        for rule in NEGATIVE_RULES:
            matched_keywords = [keyword for keyword in rule.include if _normalize_text(keyword) in text]
            if not matched_keywords:
                continue

            candidates.append(
                CandidateArticle(
                    title=article.title,
                    summary=article.summary,
                    source=article.source,
                    date=article.published_at[:10],
                    matched_keywords=matched_keywords,
                    candidate_categories=[rule.category],
                    rule_severity=rule.severity,
                    negative_news_flag=rule.flag,
                )
            )
            break

    return PrescreenResult(
        symbol=symbol,
        has_negative_candidates=bool(candidates),
        candidate_articles=candidates,
    )


def _normalize_text(text: str) -> str:
    normalized = text.lower()
    for token in (" ", "\n", "\t", "-", "_", "（", "）", "(", ")", "，", "。", "：", "、"):
        normalized = normalized.replace(token, "")
    return normalized
