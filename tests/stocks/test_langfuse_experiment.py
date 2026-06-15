from __future__ import annotations

import asyncio
import json
import sys
import types

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


def test_run_uses_async_news_filter_task(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(langfuse_news_filter_experiment, "DATASET_NAME", "news-filter-prod")
    monkeypatch.setattr(langfuse_news_filter_experiment, "EXPERIMENT_NAME", "exp-1")
    monkeypatch.setattr(langfuse_news_filter_experiment, "DESCRIPTION", "desc")
    monkeypatch.setattr(langfuse_news_filter_experiment, "CONFIG_PATH", None)
    monkeypatch.setattr(langfuse_news_filter_experiment, "MODEL_PRESET", None)
    monkeypatch.setattr(langfuse_news_filter_experiment, "MODEL_NAME_OVERRIDE", None)

    class _FakeDataset:
        def run_experiment(self, *, name: str, description: str, task, max_concurrency: int) -> dict[str, int]:
            assert name == "exp-1"
            assert description == "desc"
            assert max_concurrency == langfuse_news_filter_experiment.MAX_CONCURRENCY
            assert asyncio.iscoroutinefunction(task)
            payload = asyncio.run(task(item=types.SimpleNamespace(input={"messages": []})))
            assert json.loads(payload) == {
                "items": [{"symbol": "600000", "allowed": True, "name": "浦发银行", "risk_notes": []}],
                "partial_failures": [],
            }
            return {"total_items": 1, "success_count": 1, "failure_count": 0}

    class _FakeLangfuseClient:
        def get_dataset(self, dataset_name: str) -> _FakeDataset:
            assert dataset_name == "news-filter-prod"
            return _FakeDataset()

    fake_langfuse_module = types.SimpleNamespace(get_client=lambda: _FakeLangfuseClient())
    monkeypatch.setitem(sys.modules, "langfuse", fake_langfuse_module)

    monkeypatch.setattr(langfuse_news_filter_experiment, "load_config", lambda _path: types.SimpleNamespace())
    monkeypatch.setattr(
        langfuse_news_filter_experiment,
        "resolve_config_env_vars",
        lambda _config: types.SimpleNamespace(
            agents=types.SimpleNamespace(
                defaults=types.SimpleNamespace(
                    model_preset=None,
                    model="test-model",
                    max_tool_result_chars=2000,
                    disabled_skills=[],
                    max_tool_iterations=4,
                )
            ),
            workspace_path=".",
            tools=types.SimpleNamespace(restrict_to_workspace=True),
            resolve_preset=lambda: types.SimpleNamespace(model="test-model"),
        ),
    )
    monkeypatch.setattr(langfuse_news_filter_experiment, "make_provider", lambda _config: object())
    monkeypatch.setattr(langfuse_news_filter_experiment, "MessageBus", lambda: object())
    monkeypatch.setattr(langfuse_news_filter_experiment, "SubagentManager", lambda **_kwargs: object())

    class _FakeOrchestrator:
        def __init__(self, executor: object) -> None:
            del executor
            self.lookback_days = 7

        async def review_news_candidates(
            self,
            review_items: list[dict[str, object]],
        ) -> tuple[list[dict[str, object]], list[str]]:
            assert review_items == [{"symbol": "600000", "name": "浦发银行", "candidate_articles": []}]
            return [{"symbol": "600000", "allowed": True, "name": "浦发银行", "risk_notes": []}], []

    monkeypatch.setattr(
        langfuse_news_filter_experiment,
        "StockSelectionSubagentOrchestrator",
        _FakeOrchestrator,
    )
    monkeypatch.setattr(
        langfuse_news_filter_experiment,
        "extract_news_filter_payload",
        lambda _input: {
            "items": [{"symbol": "600000", "name": "浦发银行", "candidate_articles": []}],
            "lookback_days": 3,
        },
    )
    monkeypatch.setattr(
        langfuse_news_filter_experiment,
        "summarize_experiment_result",
        lambda **_kwargs: "summary",
    )

    assert langfuse_news_filter_experiment.run() == 0
    captured = capsys.readouterr()
    assert "summary" in captured.out
