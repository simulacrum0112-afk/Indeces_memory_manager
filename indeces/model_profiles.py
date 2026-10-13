"""Independent provider choices without credentials or resource counters."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace


STAGES = frozenset({"label", "summary", "reply", "selection", "query"})
ENDPOINTS = {"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com/v1"}
MODELS = {"openai": ("gpt-6.1-sol",), "anthropic": ("claude-opus-5-5", "claude-fable-5-1")}


def _validate(provider, profile, adapter):
    if not isinstance(profile, dict) or set(profile) != {"model", "base_url", "efforts"}:
        raise ValueError("invalid model profile fields; credentials and budgets cannot be stored in profiles")
    if profile["model"] not in MODELS[provider] or profile["base_url"] != ENDPOINTS[provider]:
        raise ValueError("model profile requires its supported official provider model and endpoint")
    efforts = profile["efforts"]
    if not isinstance(efforts, dict) or set(efforts) != STAGES:
        raise ValueError("model profile requires exactly label, summary, reply, selection and query efforts")
    # Validate inactive choices without applying their context ceiling to the
    # currently selected provider. Actual unchanged caps are checked when the
    # wizard constructs the newly selected AdapterConfig.
    budgets = {stage: replace(budget, input_tokens=1, output_tokens=1, seconds=1, reasoning=efforts[stage])
               for stage, budget in adapter.budgets.items()}
    replace(adapter, provider=provider, model=profile["model"], base_url=profile["base_url"], budgets=budgets)


def active_profile(adapter):
    return {"model": adapter.model, "base_url": adapter.base_url.rstrip("/"),
            "efforts": {stage: budget.reasoning for stage, budget in adapter.budgets.items()}}


def validate_profiles(raw, adapter):
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) != set(MODELS):
        raise ValueError("model_profiles requires independent openai and anthropic profiles")
    for provider, profile in raw.items():
        _validate(provider, profile, adapter)
    if raw[adapter.provider] != active_profile(adapter):
        raise ValueError("active model profile does not match adapter selection")
    return deepcopy(raw)


def effective_profiles(config):
    if config.model_profiles:
        return deepcopy(config.model_profiles)
    profiles = {
        "openai": {"model": "gpt-6.1-sol", "base_url": ENDPOINTS["openai"],
                   "efforts": {stage: "medium" for stage in STAGES}},
        "anthropic": {"model": "claude-opus-5-5", "base_url": ENDPOINTS["anthropic"],
                      "efforts": {stage: "high" for stage in STAGES}},
    }
    profiles[config.adapter.provider] = active_profile(config.adapter)
    for provider, profile in profiles.items():
        _validate(provider, profile, config.adapter)
    return profiles
