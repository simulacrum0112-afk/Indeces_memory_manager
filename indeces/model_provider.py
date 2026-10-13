"""Explicit model construction and provider-scoped credential selection.

Each provider uses its own environment reference, encrypted vault, then optional
private session input. Claude never reads the OpenAI vault or guesses a supplier.
These helpers themselves make no HTTP request.
"""
from __future__ import annotations

import os

from .adapter import OpenAIAdapter
from .credentials import load_anthropic_key, load_openai_key, validate_api_key


class ModelProviderError(ValueError):
    """A fixed diagnostic that cannot include a supplied credential value."""

    def __init__(self, code):
        self.code = "claude_auth_unavailable" if code == "claude_auth_unavailable" else "unsupported_model_provider"
        message = ("Claude authentication unavailable: use apikey for the selected provider or ANTHROPIC_API_KEY; OpenAI saved keys are not used"
                   if self.code == "claude_auth_unavailable" else "Unsupported model provider")
        super().__init__(message)


def _provider(config):
    # Pre-provider callers supplied the original OpenAI adapter configuration.
    # Only an absent field retains that default; explicit invalid values fail.
    provider = getattr(config, "provider", "openai")
    if not isinstance(provider, str) or provider not in {"openai", "anthropic"}:
        raise ValueError("unsupported model provider")
    return provider


def credential_reference(config) -> str:
    return "ANTHROPIC_API_KEY" if _provider(config) == "anthropic" else "OPENAI_API_KEY"


def resolve_model_key(config, config_path=None, *, prompt=None, supplied_key=None):
    """Resolve only the configured provider's authentication, with safe errors.

    Existing OpenAI environment/vault/session precedence is preserved. Claude
    has the same precedence in a separate provider-bound vault. Absence is a
    startup blocker before either model or Discord transport is constructed.
    """
    provider = _provider(config)
    key = supplied_key
    if key is None:
        key = os.environ.get(credential_reference(config))
        if not key and config_path is not None:
            key = {"openai": load_openai_key, "anthropic": load_anthropic_key}[provider](config_path)
        if not key and prompt is not None:
            provider_name = "Claude (Anthropic)" if provider == "anthropic" else "OpenAI"
            key = prompt(f"{provider_name} API key (session only, not saved; use apikey to save): ")
    if not key:
        if provider == "anthropic":
            raise ModelProviderError("claude_auth_unavailable")
        raise ValueError("OpenAI key unavailable; use apikey or OPENAI_API_KEY")
    return validate_api_key(key)


def create_model_adapter(config, scratch, key):
    """Use one construction seam for chat, watchers and maintenance launchers."""
    provider = _provider(config)
    if provider == "anthropic":
        from .claude_adapter import ClaudeAdapter
        return ClaudeAdapter(config, scratch, key)
    if provider == "openai":
        if config.model != "gpt-6.1-sol" or not isinstance(config.base_url, str) or config.base_url.rstrip("/") != "https://api.openai.com/v1":
            raise ValueError("OpenAI rollback requires its official model and endpoint")
        return OpenAIAdapter(config, scratch, key)
    raise ValueError("unsupported model provider")
