from __future__ import annotations

import json
import re
from typing import Any

from nanobot.utils.prompt_templates import render_template


def extract_messages_from_input(input_payload: Any) -> list[dict[str, Any]]:
    """Normalize dataset input into an OpenAI-compatible messages list."""
    if isinstance(input_payload, list):
        messages = input_payload
    elif isinstance(input_payload, dict) and isinstance(input_payload.get("messages"), list):
        messages = input_payload["messages"]
    else:
        raise ValueError("dataset input must be a messages list or an object with a messages field")

    if not messages:
        raise ValueError("messages must not be empty")

    normalized: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"message at index {index} must be an object")
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not role:
            raise ValueError(f"message at index {index} is missing a valid role")
        if not isinstance(content, str):
            raise ValueError(f"message at index {index} is missing string content")
        normalized.append({"role": role, "content": content})
    return normalized


def build_experiment_description(*, dataset_name: str, model: str, base_url: str | None) -> str:
    parts = [
        f"Replay real-trace dataset '{dataset_name}'",
        f"with model '{model}'",
    ]
    if base_url:
        parts.append(f"via {base_url}")
    return " ".join(parts)


def summarize_experiment_result(
    *,
    experiment_name: str,
    dataset_name: str,
    result: dict[str, Any],
) -> str:
    total_items = result.get("total_items")
    success_count = result.get("success_count")
    failure_count = result.get("failure_count")

    if all(isinstance(value, int) for value in (total_items, success_count, failure_count)):
        return (
            f"Experiment '{experiment_name}' on dataset '{dataset_name}' finished: "
            f"total={total_items}, success={success_count}, failure={failure_count}"
        )

    formatted = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    return (
        f"Experiment '{experiment_name}' on dataset '{dataset_name}' finished.\n"
        f"Result payload:\n{formatted}"
    )


def extract_text_output(response: Any) -> str:
    """Best-effort extraction of assistant text from common OpenAI SDK responses."""
    choices = getattr(response, "choices", None)
    if isinstance(choices, list) and choices:
        first_choice = choices[0]
        message = getattr(first_choice, "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content

    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return output_text

    raise ValueError("unable to extract text content from model response")


def extract_news_filter_payload(input_payload: Any) -> dict[str, Any]:
    """Extract review items and lookback window from a trace-style news-filter input."""
    messages = extract_messages_from_input(input_payload)
    user_contents = [message["content"] for message in messages if message["role"] == "user"]
    if not user_contents:
        raise ValueError("news-filter input must include a user message")

    user_content = user_contents[-1]
    items_match = re.search(r"items=(\[.*\])\s*lookback_days=", user_content, re.DOTALL)
    lookback_match = re.search(r"lookback_days=(\d+)", user_content)

    if not items_match:
        raise ValueError("unable to extract items from news-filter user message")
    if not lookback_match:
        raise ValueError("unable to extract lookback_days from news-filter user message")

    items = json.loads(items_match.group(1))
    lookback_days = int(lookback_match.group(1))

    if not isinstance(items, list):
        raise ValueError("news-filter items must be a JSON array")

    return {
        "items": items,
        "lookback_days": lookback_days,
    }


def build_news_filter_messages(*, items: list[dict[str, Any]], lookback_days: int) -> list[dict[str, str]]:
    system_prompt = render_template("stocks/system/news_filter.md", strip=True)
    user_prompt = render_template(
        "stocks/tasks/news_filter.md",
        strip=True,
        items_json=json.dumps(items, ensure_ascii=False),
        lookback_days=lookback_days,
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
