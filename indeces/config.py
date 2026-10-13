from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
import os
from pathlib import Path
import tomllib

from .identity import AGENT_NAME, normalize_agent_name
from .path_policy import (validate_config_path, validate_knowledge_root,
                          validate_managed_path, validate_runtime_paths)


@dataclass(frozen=True)
class Budget:
    input_tokens: int
    output_tokens: int
    seconds: float
    reasoning: str = "medium"

    def __post_init__(self):
        if type(self.input_tokens) is not int or not 1 <= self.input_tokens <= 1_050_000:
            raise ValueError("invalid input_tokens")
        if type(self.output_tokens) is not int or not 1 <= self.output_tokens <= 128_000:
            raise ValueError("invalid output_tokens")
        if self.input_tokens + self.output_tokens > 1_050_000:
            raise ValueError("input plus output exceeds model context")
        if type(self.seconds) not in (float, int) or not math.isfinite(self.seconds) or not 0 < self.seconds <= 300:
            raise ValueError("invalid stage deadline")
        if not isinstance(self.reasoning, str) or self.reasoning not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("invalid reasoning effort")


@dataclass(frozen=True)
class DiscordConfig:
    guild_id: str
    channel_ids: tuple[str, ...] = ()
    queue_capacity: int = 16
    delivery_seconds: float = 10.0


@dataclass(frozen=True)
class RuntimeConfig:
    turn_seconds: float = 130.0
    queue_wait_seconds: float = 120.0
    local_seconds: float = 5.0
    summary_max_bytes: int = 4096
    low_watermark: float = 0.70
    retrieval_policy: str = 'concept_v1'
    model_selection_enabled: bool = True
    richard_selection_enabled: bool = False
    richard_graph_zip: str = ""
    richard_source_zip: str = ""
    active_requery_enabled: bool = False
    active_requery_max_rounds: int = 2
    active_requery_max_calls: int = 6
    active_requery_input_tokens: int = 32768
    active_requery_output_tokens: int = 4096
    active_requery_seconds: float = 60.0
    active_requery_cost_usd: float = 0.0
    active_requery_allow_real_calls: bool = False
    active_requery_input_usd_per_million: float = 0.0
    active_requery_output_usd_per_million: float = 0.0
    communicability_enabled: bool = False
    path_hypotheses_enabled: bool = False
    path_requery_loop_enabled: bool = False
    richard_chat_enabled: bool = False
    richard_chat_algorithm: str = "alpha"
    prune_profile_config: str = ""

    def __post_init__(self):
        if (type(self.prune_profile_config) is not str or len(self.prune_profile_config) > 4096
                or any(ord(char) < 32 for char in self.prune_profile_config)
                or (self.prune_profile_config and not self.prune_profile_config.strip())):
            raise ValueError("runtime.prune_profile_config must be a bounded configuration path")
        if self.prune_profile_config and (self.active_requery_enabled or self.path_requery_loop_enabled):
            raise ValueError("prune chat does not support simultaneous active/path requery")
        if type(self.model_selection_enabled) is not bool:
            raise ValueError("runtime.model_selection_enabled must be a boolean")
        if type(self.richard_selection_enabled) is not bool:
            raise ValueError("runtime.richard_selection_enabled must be a boolean")
        if type(self.richard_chat_enabled) is not bool:
            raise ValueError("runtime.richard_chat_enabled must be a boolean")
        if type(self.richard_chat_algorithm) is not str or self.richard_chat_algorithm not in ("alpha", "beta"):
            raise ValueError("runtime.richard_chat_algorithm must be alpha or beta")
        if self.richard_chat_enabled and self.richard_selection_enabled:
            raise ValueError("Richard chat and legacy Richard selection are mutually exclusive")
        for key in ("richard_graph_zip", "richard_source_zip"):
            value = getattr(self, key)
            if type(value) is not str or "\0" in value or (value and not value.strip()):
                raise ValueError(f"runtime.{key} must be a path string")
        if self.richard_selection_enabled:
            if self.model_selection_enabled:
                raise ValueError("Richard selection requires model_selection_enabled=false")
            if not self.richard_graph_zip or not self.richard_source_zip:
                raise ValueError("Richard selection requires both pinned ZIP paths")
        if self.richard_chat_enabled:
            if not self.model_selection_enabled:
                raise ValueError("Richard chat requires model_selection_enabled=true")
            if not self.richard_graph_zip or not self.richard_source_zip:
                raise ValueError("Richard chat requires both pinned ZIP paths")
        for key in ("active_requery_enabled", "active_requery_allow_real_calls", "communicability_enabled",
                    "path_hypotheses_enabled", "path_requery_loop_enabled"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"runtime.{key} must be a boolean")
        for key, upper in (("active_requery_max_rounds", 4), ("active_requery_max_calls", 12),
                           ("active_requery_input_tokens", 65536), ("active_requery_output_tokens", 8192)):
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError(f"invalid runtime.{key}")
        if (type(self.active_requery_seconds) not in (int, float)
                or not 0 < self.active_requery_seconds <= 130
                or not math.isfinite(self.active_requery_seconds)):
            raise ValueError("invalid active requery deadline")
        for key in ("active_requery_cost_usd", "active_requery_input_usd_per_million",
                    "active_requery_output_usd_per_million"):
            value = getattr(self, key)
            if type(value) not in (int, float) or not 0 <= value <= 1000 or not math.isfinite(value):
                raise ValueError(f"invalid runtime.{key}")


@dataclass(frozen=True)
class AdapterConfig:
    model: str
    base_url: str
    budgets: dict[str, Budget]
    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    verbosity: str = "high"
    provider: str = "openai"

    def __post_init__(self):
        # A separate post-retrieval selection call keeps the three established
        # stage budgets intact. Older configuration files need no rewrite.
        if "selection" not in self.budgets:
            budgets = dict(self.budgets)
            label = budgets.get("label", Budget(4096, 512, 15.0))
            reply = budgets.get("reply", label)
            budgets["selection"] = Budget(min(4096, label.input_tokens),
                min(512, label.output_tokens), min(15.0, label.seconds), reply.reasoning)
            object.__setattr__(self, "budgets", budgets)
        # Separate action planning, never a renamed post-retrieval selector.
        if "query" not in self.budgets:
            budgets = dict(self.budgets)
            reply = budgets.get("reply", budgets.get("label", Budget(4096, 512, 15.0)))
            budgets["query"] = Budget(min(4096, reply.input_tokens), min(512, reply.output_tokens),
                                      min(15.0, reply.seconds), reply.reasoning)
            object.__setattr__(self, "budgets", budgets)
        if not isinstance(self.provider, str) or self.provider not in {"openai", "anthropic"}:
            raise ValueError("unsupported model provider")
        if self.provider == "anthropic":
            if self.model not in {"claude-opus-5-5", "claude-fable-5-1"}:
                raise ValueError("Claude requires claude-opus-5-5 or claude-fable-5-1")
            if not isinstance(self.base_url, str) or self.base_url.rstrip("/") != "https://api.anthropic.com/v1":
                raise ValueError("Claude native Messages requires an explicit official Anthropic endpoint")
            if any(b.reasoning != "high" for b in self.budgets.values()):
                raise ValueError("Claude requires explicit high effort for every stage")
            if any(b.input_tokens + b.output_tokens > 1_000_000 for b in self.budgets.values()):
                raise ValueError("Claude input plus output exceeds model context")
        if not isinstance(self.verbosity, str) or self.verbosity not in {"low", "medium", "high"}:
            raise ValueError("invalid output verbosity")
        if self.model == "gpt-6.1-sol" and any(b.reasoning == "none" for b in self.budgets.values()):
            raise ValueError("gpt-6.1-sol does not support reasoning effort none")


@dataclass(frozen=True)
class KnowledgeConfig:
    poll_seconds: float = 0.5
    max_file_bytes: int = 8192
    max_files: int = 256
    chunk_characters: int = 400
    version_seconds: float = 1080.0
    version_input_tokens: int = 196608
    version_output_tokens: int = 49152

    def __post_init__(self):
        # Direct construction and TOML loading share the same protection
        # contract. max_file_bytes is the original UTF-8 text input limit;
        # PDF input and converted Markdown have independent PdfConfig bounds.
        for name in ("poll_seconds", "version_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 3600:
                raise ValueError(f"knowledge.{name} must be finite and in (0, 3600]")
        for name in ("max_file_bytes", "max_files", "chunk_characters", "version_input_tokens", "version_output_tokens"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"knowledge.{name} must be a positive integer")
        for name, upper in (("max_file_bytes", 1024 * 1024), ("max_files", 1024), ("chunk_characters", 4000)):
            if getattr(self, name) > upper:
                raise ValueError(f"knowledge.{name} exceeds supported bound {upper}")

    def file_limit(self, suffix, pdf):
        """Return the actual source-byte limit and its unambiguous config key."""
        if suffix.lower() == ".pdf":
            return pdf.max_file_bytes, "pdf.max_file_bytes"
        return self.max_file_bytes, "knowledge.max_file_bytes"


@dataclass(frozen=True)
class PdfConfig:
    """Local conversion limits; they do not borrow a model stage's budget."""
    max_file_bytes: int = 16 * 1024 * 1024
    max_pages: int = 200
    max_markdown_bytes: int = 512 * 1024
    seconds: float = 30.0

    def __post_init__(self):
        for value, lower, upper in ((self.max_file_bytes, 1024, 128 * 1024 * 1024),
                                    (self.max_pages, 1, 2000),
                                    (self.max_markdown_bytes, 1024, 4 * 1024 * 1024)):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("invalid PDF conversion bound")
        if (type(self.seconds) not in (int, float) or not math.isfinite(self.seconds)
                or not 0 < self.seconds <= 300):
            raise ValueError("invalid PDF conversion deadline")


@dataclass(frozen=True)
class SessionBudgetConfig:
    enabled: bool = False
    allow_real_calls: bool = False
    session_id: str = ""
    ceiling_usd: str = "5.00"
    model: str = ""
    base_url: str = ""
    input_usd_per_million: str = ""
    output_usd_per_million: str = ""
    per_call_usd: str = ""
    reference: str = ""
    valid_until_utc: str = ""
    count_fee_verified: bool = False
    count_fee_reference: str = ""

    def __post_init__(self):
        for name in ("enabled", "allow_real_calls", "count_fee_verified"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"session_budget.{name} must be a boolean")
        for name in set(self.__dataclass_fields__) - {"enabled", "allow_real_calls", "count_fee_verified"}:
            value = getattr(self, name)
            if type(value) is not str or len(value) > 256 or "\0" in value:
                raise ValueError(f"session_budget.{name} must be a bounded string")
            value.encode("utf-8")


def controlled_session_enabled(config):
    settings = getattr(config, "session_budget", None)
    enabled = getattr(settings, "enabled", False)
    if type(enabled) is not bool:
        raise ValueError("session_budget.enabled must be a boolean")
    return enabled


def guard_trial_maintenance(config):
    if controlled_session_enabled(config) or getattr(config.runtime, "prune_profile_config", ""):
        from .contracts import GovernedError
        raise GovernedError("session_maintenance_forbidden")


@dataclass(frozen=True)
class Config:
    name: str
    state_dir: Path
    scratch_dir: Path
    knowledge_dir: Path
    discord: DiscordConfig
    runtime: RuntimeConfig
    adapter: AdapterConfig
    knowledge: KnowledgeConfig
    pdf: PdfConfig = field(default_factory=PdfConfig)
    session_budget: SessionBudgetConfig = field(default_factory=SessionBudgetConfig)
    model_profiles: dict = field(default_factory=dict)


def _keys(data, allowed):
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise ValueError("unknown configuration field")


def snowflake(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 20
            or any(c not in "0123456789" for c in value)
            or not 0 < int(value) < 2**64):
        raise ValueError("invalid Discord snowflake ID")
    return str(int(value))


def load_config(path: Path, *, _allow_prune_profile=True) -> Config:
    path = validate_config_path(path)
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    _keys(raw, {"name", "state_dir", "scratch_dir", "knowledge_dir", "discord", "runtime", "adapter", "knowledge", "pdf", "session_budget", "model_profiles"})
    d = raw["discord"]
    r = raw.get("runtime", {})
    a = raw["adapter"]
    k = raw.get("knowledge", {})
    p = raw.get("pdf", {})
    s = raw.get("session_budget", {})
    _keys(d, {"guild_id", "channel_ids", "queue_capacity", "delivery_seconds"})
    _keys(r, RuntimeConfig.__dataclass_fields__)
    _keys(a, AdapterConfig.__dataclass_fields__)
    _keys(k, KnowledgeConfig.__dataclass_fields__)
    _keys(p, PdfConfig.__dataclass_fields__)
    _keys(s, SessionBudgetConfig.__dataclass_fields__)
    if not isinstance(d.get("guild_id"), str):
        raise ValueError("guild_id must be a numeric string")
    guild_id = snowflake(d["guild_id"]) if d["guild_id"] else ""
    channels = d.get("channel_ids", [])
    if not isinstance(channels, list):
        raise ValueError("channel_ids must be numeric strings")
    dc = DiscordConfig(**{**d, "guild_id": guild_id, "channel_ids": tuple(snowflake(x) for x in channels)})
    rc = RuntimeConfig(**r)
    if rc.prune_profile_config:
        if not _allow_prune_profile:
            raise ValueError("recursive prune profile references are forbidden")
        profile_path = validate_config_path(path.parent / rc.prune_profile_config)
        rc = replace(rc, prune_profile_config=str(profile_path))
    if rc.richard_selection_enabled or rc.richard_chat_enabled:
        rc = replace(rc, **{key: str(validate_managed_path(path.parent / getattr(rc, key), key))
                            for key in ("richard_graph_zip", "richard_source_zip")})
    if rc.retrieval_policy not in ('legacy_v1', 'concept_v1'):
        raise ValueError('invalid retrieval policy')
    kc = KnowledgeConfig(**k)
    pc = PdfConfig(**p)
    sc = SessionBudgetConfig(**s)
    if type(dc.queue_capacity) is not int or not 1 <= dc.queue_capacity <= 256:
        raise ValueError("invalid queue capacity")
    for seconds in (dc.delivery_seconds, rc.turn_seconds, rc.queue_wait_seconds, rc.local_seconds, a.get("cooldown_seconds", 30)):
        if type(seconds) not in (float, int) or not math.isfinite(seconds) or not 0 < seconds <= 3600:
            raise ValueError("invalid deadline")
    if type(rc.summary_max_bytes) is not int or not 128 <= rc.summary_max_bytes <= 65536:
        raise ValueError("invalid summary bound")
    if type(rc.low_watermark) not in (float, int) or not 0 < rc.low_watermark < 1:
        raise ValueError("invalid low watermark")
    if not isinstance(a.get("base_url"), str) or not a["base_url"]:
        raise ValueError("an explicit provider endpoint is required")
    if a.get("provider", "openai") == "openai" and (a["model"] != "gpt-6.1-sol" or a["base_url"].rstrip("/") != "https://api.openai.com/v1"):
        raise ValueError("this adaptor supports official OpenAI gpt-6.1-sol only; migrate the model explicitly")
    if type(a.get("failure_threshold", 3)) is not int or not 1 <= a.get("failure_threshold", 3) <= 20:
        raise ValueError("invalid circuit failure threshold")
    budgets = a["budgets"]
    if not {"label", "summary", "reply"} <= set(budgets) or set(budgets) - {"label", "summary", "reply", "selection", "query"}:
        raise ValueError("label, summary and reply budgets required; selection and query are optional")
    ac = AdapterConfig(**{**a, "budgets": {k: Budget(**v) for k, v in budgets.items()}})
    from .model_profiles import validate_profiles
    profiles = validate_profiles(raw.get("model_profiles"), ac)
    if rc.prune_profile_config:
        if sc.enabled:
            raise ValueError("prune chat requires its existing grant; separate session budget is incompatible")
        selected_profile = load_config(Path(rc.prune_profile_config), _allow_prune_profile=False)
        if selected_profile.adapter.provider != "anthropic":
            raise ValueError("prune chat requires the selected native Claude provider")
        ac = selected_profile.adapter
        profiles = selected_profile.model_profiles
    name = raw.get("name", AGENT_NAME)
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("invalid agent name")
    name = normalize_agent_name(name)
    def directory(key):
        value = Path(raw.get(key, key.removesuffix("_dir")))
        return path.parent / value
    config = Config(name, directory("state_dir"), directory("scratch_dir"), directory("knowledge_dir"), dc, rc, ac, kc, pc, sc, profiles)
    validate_runtime_paths(config)
    knowledge_dir = validate_knowledge_root(config, expected=path.parent / "knowledge")
    return Config(name, Path(os.path.abspath(config.state_dir)), Path(os.path.abspath(config.scratch_dir)),
                  knowledge_dir, dc, rc, ac, kc, pc, sc, profiles)
