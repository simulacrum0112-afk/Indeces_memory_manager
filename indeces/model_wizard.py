"""Stopped-service, reviewed provider/model selection; no keys or HTTP calls."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import re
import tomllib
import uuid

from .config import controlled_session_enabled, load_config
from .discord_wizard import _atomic_write, _comment, _complete
from .lock import InstanceLock
from .model_profiles import ENDPOINTS, MODELS, STAGES, effective_profiles, validate_profiles
from .path_policy import validate_config_path, validate_managed_path


class ModelWizardError(ValueError):
    pass


def _receipt(output, text):
    try:
        output(text)
    except (Exception, KeyboardInterrupt):
        pass


def patch_model_config(contents, config, provider, model):
    """Preserve all values except selected provider/model/efforts and profiles.

    Standard or quoted TOML table headers are supported. Ambiguous inline or
    multiline profile layouts decline before writing rather than reformatting
    unrelated configuration. Profiles never carry credentials or numeric caps.
    """
    if provider not in MODELS or model not in MODELS[provider]:
        raise ModelWizardError("unsupported_model_selection")
    if controlled_session_enabled(config) and (provider != config.adapter.provider or model != config.adapter.model):
        raise ModelWizardError("controlled_session_model_switch_forbidden")
    before = tomllib.loads(contents.decode("utf-8"))
    profiles = effective_profiles(config)
    profiles[provider]["model"] = model
    selected = profiles[provider]
    candidate = replace(config.adapter, provider=provider, model=model, base_url=selected["base_url"],
                        budgets={stage: replace(budget, reasoning=selected["efforts"][stage])
                                 for stage, budget in config.adapter.budgets.items()})
    validate_profiles(profiles, candidate)
    expected = deepcopy(before)
    expected["adapter"].update(provider=provider, model=model, base_url=selected["base_url"])
    materialized = set()
    for stage, effort in selected["efforts"].items():
        if stage not in expected["adapter"]["budgets"]:
            # Optional selection/query tables inherit only their already-loaded
            # effective caps. The switch cannot create a larger allowance.
            expected["adapter"]["budgets"][stage] = asdict(config.adapter.budgets[stage])
            materialized.add(stage)
        expected["adapter"]["budgets"][stage]["reasoning"] = effort
    expected["model_profiles"] = deepcopy(profiles)

    text = contents.decode("utf-8")
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    targets = {(("adapter",), "provider"): provider, (("adapter",), "model"): model,
               (("adapter",), "base_url"): selected["base_url"]}
    for stage, effort in selected["efforts"].items():
        if stage not in materialized:
            targets[(("adapter", "budgets", stage), "reasoning")] = effort
    if "model_profiles" in before:
        for key, profile in profiles.items():
            for field in ("model", "base_url"):
                targets[(("model_profiles", key), field)] = profile[field]
            for stage, effort in profile["efforts"].items():
                targets[(("model_profiles", key, "efforts"), stage)] = effort

    edits, found, ends = [], set(), {}
    section = ()
    key_pattern = re.compile(r'''^(\s*(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')\s*=\s*)''')
    for index, line in enumerate(lines):
        if not _complete("".join(lines[:index])):
            continue
        if re.match(r"^\s*\[.*\]\s*(?:#.*)?$", line.rstrip("\r\n")):
            try:
                tree = tomllib.loads(line)
                path = []
                while isinstance(tree, dict) and len(tree) == 1:
                    key, tree = next(iter(tree.items()))
                    path.append(key)
                if tree != {}:
                    raise ModelWizardError("unsupported_model_table_layout")
            except tomllib.TOMLDecodeError:
                raise ModelWizardError("unsupported_model_table_layout") from None
            ends[section] = index
            section = tuple(path)
            continue
        match = key_pattern.match(line)
        if not match:
            continue
        lhs = match.group(1)
        key = next(iter(tomllib.loads(lhs + '"probe"')))
        target = (section, key)
        if target not in targets:
            continue
        end = index + 1
        while end <= len(lines) and not _complete("".join(lines[index:end])):
            end += 1
        if end > len(lines):
            raise ModelWizardError("unsupported_model_value_layout")
        edits.append((index, end, lhs + json.dumps(targets[target]) + _comment(lines[end - 1]) + newline))
        found.add(target)
    ends[section] = len(lines)
    additions = {}
    for target, value in targets.items():
        if target in found:
            continue
        section, key = target
        if section not in ends:
            raise ModelWizardError("unsupported_model_profile_layout")
        additions.setdefault(ends[section], []).append(f"{key} = {json.dumps(value)}{newline}")
    for index, values in additions.items():
        prefix = newline if index and not lines[index - 1].endswith(("\n", "\r")) else ""
        edits.append((index, index, prefix + "".join(values)))
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start:end] = [replacement]
    result = "".join(lines)
    for stage in sorted(materialized):
        result += newline + f"[adapter.budgets.{stage}]{newline}"
        for key, value in expected["adapter"]["budgets"][stage].items():
            result += f"{key} = {json.dumps(value)}{newline}"
    if "model_profiles" not in before:
        result += newline
        for key in ("openai", "anthropic"):
            profile = profiles[key]
            result += (f"[model_profiles.{key}]{newline}model = {json.dumps(profile['model'])}{newline}"
                       f"base_url = {json.dumps(profile['base_url'])}{newline}"
                       f"[model_profiles.{key}.efforts]{newline}")
            for stage in sorted(STAGES):
                result += f"{stage} = {json.dumps(profile['efforts'][stage])}{newline}"
    if tomllib.loads(result) != expected:
        raise ModelWizardError("configuration_scope_changed")
    return result.encode("utf-8")


def _backup_config(path, original):
    backup = validate_managed_path(path.with_name(path.name + ".before-model-" + uuid.uuid4().hex + ".bak"), "model configuration backup")
    with backup.open("xb") as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    return backup


def configure_model(config_path, input_fn=None, output=None):
    input_fn, output = input_fn or input, output or print
    lease = None
    committed = False
    try:
        path = validate_config_path(config_path)
        original = path.read_bytes()
        config = load_config(path)
        try:
            lease = InstanceLock(config.state_dir)
        except RuntimeError:
            output("Model setup unavailable: stop the running instance using this state directory first. No hot reload or restart is performed.")
            return False
        if path.read_bytes() != original:
            raise ModelWizardError("configuration_changed_during_setup")
        profiles = effective_profiles(config)
        output("Indeces model selection. OpenAI and Claude keep independent profiles and encrypted keys. No API call or service starts here.")
        names = {"openai": "OpenAI", "anthropic": "Claude (Anthropic)"}
        output(f"Current: provider={names[config.adapter.provider]}; model={config.adapter.model}")
        while True:
            provider = input_fn(f"Provider OpenAI/openai or Claude/claude [{config.adapter.provider}]: ").strip().lower() or config.adapter.provider
            provider = "anthropic" if provider == "claude" else provider
            if provider in MODELS:
                break
            output("Choose openai or claude (anthropic also accepted).")
        default = profiles[provider]["model"]
        if provider == "anthropic":
            output("Claude choices: Opus 5.5 = claude-opus-5-5; Fable 5.1 = claude-fable-5-1. Both use high effort.")
        while True:
            model = input_fn(f"Model {', '.join(MODELS[provider])} [{default}]: ").strip() or default
            if model in MODELS[provider]:
                break
            output("Choose one of the exact model IDs shown.")
        updated = patch_model_config(original, config, provider, model)
        output(f"Review: provider={names[provider]}; model={model}; endpoint={ENDPOINTS[provider]}; efforts={profiles[provider]['efforts']}")
        output("All token/time caps, state/knowledge data and both saved keys are preserved. The selected provider takes effect only on your next explicit start.")
        if provider == "anthropic" and model == "claude-fable-5-1":
            output("Fable 5.1 live use also requires the official workspace retention compatibility; this setup does not change retention.")
        if input_fn("Save this model selection? [y/N]: ").strip().lower() not in {"y", "yes"}:
            output("Cancelled; model selection and keys unchanged.")
            return False
        if validate_config_path(path) != path or path.read_bytes() != original:
            raise ModelWizardError("configuration_changed_during_setup")
        backup = _backup_config(path, original)
        # Backup/fsync can give a manual editor time to change the config. Its
        # edit is preserved; only our prepared bytes may enter rollback below.
        if validate_config_path(path) != path or path.read_bytes() != original:
            raise ModelWizardError("configuration_changed_during_setup")
        try:
            _atomic_write(path, updated)
        except BaseException as error:
            try:
                current = path.read_bytes()
                if current == updated:
                    _atomic_write(path, original)
                elif current != original:
                    raise ModelWizardError("configuration_changed_during_rollback")
            except BaseException:
                _receipt(output, "Model configuration rollback incomplete; review the retained before-model backup before starting.")
                return False
            _receipt(output, "Model selection was not saved; prior configuration preserved.")
            if isinstance(error, (KeyboardInterrupt, EOFError)):
                return False
            raise ModelWizardError("configuration_save_failed") from None
        committed = True
        _receipt(output, f"Saved model selection; backup={backup.name}. Use apikey for the selected provider, then start when ready. No service was started.")
        return True
    except (EOFError, KeyboardInterrupt):
        _receipt(output, "Model selection was saved before cancellation." if committed else "Cancelled; model selection and keys unchanged.")
        return committed
    except (ValueError, OSError) as error:
        if committed:
            return True
        code = str(error) if isinstance(error, ModelWizardError) else "configuration_or_permissions"
        _receipt(output, f"Model setup failed: {code}; no service was started.")
        return False
    finally:
        if lease is not None:
            try:
                lease.close()
            except Exception:
                _receipt(output, "Model setup lease cleanup failed; close this Console before start.")
