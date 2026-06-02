"""Helpers for normalizing raw news adapter output for negative-news filtering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class RawNewsArticle(Protocol):
    title: str
    summary: str
    published_at: str
    source: str


@dataclass(frozen=True)
class AdaptedNewsArticle:
    """Normalized article shape used by news rules."""

    title: str
    summary: str
    published_at: str
    source: str

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.summary}".strip()


def adapt_news_articles(articles: list[RawNewsArticle]) -> list[AdaptedNewsArticle]:
    """Normalize article fields and drop obvious duplicates."""

    adapted: list[AdaptedNewsArticle] = []
    seen: set[tuple[str, str, str]] = set()

    for article in articles:
        title = article.title.strip()
        summary = article.summary.strip()
        published_at = article.published_at.strip()
        source = article.source.strip() or "unknown"

        if not title and not summary:
            continue

        identity = (title, summary, published_at)
        if identity in seen:
            continue
        seen.add(identity)

        adapted.append(
            AdaptedNewsArticle(
                title=title,
                summary=summary,
                published_at=published_at,
                source=source,
            )
        )

    return adapted
