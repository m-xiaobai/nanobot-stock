from __future__ import annotations

import json

import pytest

from experiment import langfuse_news_filter_experiment
from experiment.langfuse_experiment import (
    build_experiment_description,
    extract_messages_from_input,
    extract_news_filter_payload,
    summarize_experiment_result,
)


def test_extract_messages_from_list_input_returns_original_messages() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]

    assert extract_messages_from_input(messages) == messages


def test_extract_messages_from_object_input_reads_messages_field() -> None:
    payload = {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ],
        "metadata": {"stage": "news-filter"},
    }

    assert extract_messages_from_input(payload) == payload["messages"]


def test_extract_messages_from_invalid_input_raises_value_error() -> None:
    with pytest.raises(ValueError, match="messages"):
        extract_messages_from_input({"items": []})


def test_extract_news_filter_payload_reads_items_and_lookback_from_user_message() -> None:
    payload = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": (
                "任务：评估所提供的预筛选股票在回看窗口内是否存在重大负面新闻风险。\n"
                'items=[{"symbol":"603262","name":"技源集团","candidate_articles":[]}]\n'
                "lookback_days=7"
            ),
        },
    ]

    extracted = extract_news_filter_payload(payload)

    assert extracted["items"] == [{"symbol": "603262", "name": "技源集团", "candidate_articles": []}]
    assert extracted["lookback_days"] == 7


def test_build_experiment_description_includes_core_fields() -> None:
    description = build_experiment_description(
        dataset_name="news-filter-prod",
        model="gpt-4.1",
        base_url="https://api.openai.com/v1",
    )

    assert "news-filter-prod" in description
    assert "gpt-4.1" in description
    assert "https://api.openai.com/v1" in description


def test_summarize_experiment_result_formats_known_counts() -> None:
    summary = summarize_experiment_result(
        experiment_name="news-filter-v1",
        dataset_name="prod-dataset",
        result={
            "run_name": "news-filter-v1",
            "dataset_name": "prod-dataset",
            "total_items": 12,
            "success_count": 10,
            "failure_count": 2,
        },
    )

    assert "news-filter-v1" in summary
    assert "prod-dataset" in summary
    assert "12" in summary
    assert "10" in summary
    assert "2" in summary


def test_summarize_experiment_result_falls_back_to_json() -> None:
    summary = summarize_experiment_result(
        experiment_name="exp",
        dataset_name="dataset",
        result={"custom": {"nested": True}},
    )

    assert json.dumps({"custom": {"nested": True}}, ensure_ascii=False, indent=2) in summary


def test_build_settings_uses_module_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(langfuse_news_filter_experiment, "DATASET_NAME", "news-filter-prod")
    monkeypatch.setattr(langfuse_news_filter_experiment, "EXPERIMENT_NAME", "exp-1")
    monkeypatch.setattr(langfuse_news_filter_experiment, "MAX_CONCURRENCY", 3)
    monkeypatch.setattr(langfuse_news_filter_experiment, "DESCRIPTION", None)
    monkeypatch.setattr(langfuse_news_filter_experiment, "CONFIG_PATH", "~/.nanobot/config.json")
    monkeypatch.setattr(langfuse_news_filter_experiment, "MODEL_PRESET", "news-filter-preset")
    monkeypatch.setattr(langfuse_news_filter_experiment, "MODEL_NAME_OVERRIDE", "gpt-4.1")

    settings = langfuse_news_filter_experiment.build_settings()

    assert settings["dataset_name"] == "news-filter-prod"
    assert settings["experiment_name"] == "exp-1"
    assert settings["max_concurrency"] == 3
    assert settings["config_path"] == "~/.nanobot/config.json"
    assert settings["model_preset"] == "news-filter-preset"
    assert settings["model_name_override"] == "gpt-4.1"
    assert "news-filter-prod" in settings["description"]


def test_build_settings_rejects_placeholder_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(langfuse_news_filter_experiment, "DATASET_NAME", "your-dataset-name")

    with pytest.raises(RuntimeError, match="DATASET_NAME"):
        langfuse_news_filter_experiment.build_settings()
