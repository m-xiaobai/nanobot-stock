from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from experiment.langfuse_experiment import (
    build_experiment_description,
    extract_news_filter_payload,
    summarize_experiment_result,
)
from nanobot.agent.subagent import SubagentManager
from nanobot.bus.queue import MessageBus
from nanobot.config.loader import load_config, resolve_config_env_vars
from nanobot.providers.factory import make_provider
from nanobot.stocks.orchestrator import StockSelectionSubagentOrchestrator

# Edit these variables directly before running the script.
DATASET_NAME = "news-filter/groundedness_judge"
EXPERIMENT_NAME: str | None = None
MAX_CONCURRENCY = 4
DESCRIPTION: str | None = None
CONFIG_PATH: str | None = None
MODEL_PRESET: str | None = None
MODEL_NAME_OVERRIDE: str | None = None


def build_experiment_name(dataset_name: str, explicit_name: str | None) -> str:
    if explicit_name:
        return explicit_name
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{dataset_name}-replay-{timestamp}"


def build_settings() -> dict[str, Any]:
    if not DATASET_NAME or DATASET_NAME == "your-dataset-name":
        raise RuntimeError("Please set DATASET_NAME in the script before running it")

    experiment_name = build_experiment_name(DATASET_NAME, EXPERIMENT_NAME)
    description = DESCRIPTION or build_experiment_description(
        dataset_name=DATASET_NAME,
        model=MODEL_NAME_OVERRIDE or MODEL_PRESET or "config-default",
        base_url=None,
    )

    return {
        "dataset_name": DATASET_NAME,
        "experiment_name": experiment_name,
        "max_concurrency": MAX_CONCURRENCY,
        "description": description,
        "config_path": CONFIG_PATH,
        "model_preset": MODEL_PRESET,
        "model_name_override": MODEL_NAME_OVERRIDE,
    }


# class _ExperimentExecutor:
#     def __init__(self, *, model: str, temperature: float, client: Any) -> None:
#         self._model = model
#         self._temperature = temperature
#         self._client = client
#
#     async def run_inline(
#         self,
#         *,
#         task: str,
#         label: str,
#         temperature: float | None = None,
#         extra_system_prompt: str | None = None,
#         allow_builtin_tools: bool = True,
#         allow_mcp_tools: bool = True,
#         use_lightweight_system_prompt: bool = False,
#     ) -> str:
#         del label, allow_builtin_tools, allow_mcp_tools, use_lightweight_system_prompt
#         messages = []
#         if extra_system_prompt:
#             messages.append({"role": "system", "content": extra_system_prompt})
#         messages.append({"role": "user", "content": task})
#         response = self._client.chat.completions.create(
#             model=self._model,
#             messages=messages,
#             temperature=self._temperature if temperature is None else temperature,
#             response_format={"type": "json_object"},
#         )
#         choices = getattr(response, "choices", None)
#         if isinstance(choices, list) and choices:
#             first_choice = choices[0]
#             message = getattr(first_choice, "message", None)
#             content = getattr(message, "content", None)
#             if isinstance(content, str):
#                 return content
#         output_text = getattr(response, "output_text", None)
#         if isinstance(output_text, str):
#             return output_text
#         raise ValueError("unable to extract text content from model response")


def run() -> int:
    try:
        from langfuse import get_client
    except Exception as exc:  # pragma: no cover - dependency/runtime guard
        raise RuntimeError("langfuse is required to run this script") from exc

    settings = build_settings()
    config_path = Path(settings["config_path"]).expanduser() if settings["config_path"] else None
    config = resolve_config_env_vars(load_config(config_path))
    if settings["model_preset"]:
        config.agents.defaults.model_preset = settings["model_preset"]
    if settings["model_name_override"]:
        config.agents.defaults.model = settings["model_name_override"]
        config.agents.defaults.model_preset = None

    provider = make_provider(config)
    bus = MessageBus()
    executor = SubagentManager(
        provider=provider,
        workspace=config.workspace_path,
        bus=bus,
        model=config.resolve_preset().model,
        tools_config=config.tools,
        max_tool_result_chars=config.agents.defaults.max_tool_result_chars,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        disabled_skills=config.agents.defaults.disabled_skills,
        max_iterations=config.agents.defaults.max_tool_iterations,
    )
    orchestrator = StockSelectionSubagentOrchestrator(executor=executor)
    langfuse = get_client()
    dataset = langfuse.get_dataset(settings["dataset_name"])

    def news_filter_task(*, item, **_kwargs):
        payload = extract_news_filter_payload(item.input)
        original_lookback = orchestrator.lookback_days
        orchestrator.lookback_days = payload["lookback_days"]
        try:
            reviewed_items, partial_failures = asyncio.run(
                orchestrator.review_news_candidates(payload["items"])
            )
        finally:
            orchestrator.lookback_days = original_lookback

        return json.dumps(
            {
                "items": reviewed_items,
                "partial_failures": partial_failures,
            },
            ensure_ascii=False,
        )

    result = dataset.run_experiment(
        name=settings["experiment_name"],
        description=settings["description"],
        task=news_filter_task,
        max_concurrency=settings["max_concurrency"],
    )

    payload: dict[str, Any]
    if hasattr(result, "model_dump"):
        payload = result.model_dump()
    elif hasattr(result, "dict"):
        payload = result.dict()
    elif isinstance(result, dict):
        payload = result
    else:
        payload = {"result": str(result)}

    print(
        summarize_experiment_result(
            experiment_name=settings["experiment_name"],
            dataset_name=settings["dataset_name"],
            result=payload,
        )
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
