from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import tomllib

from .identity import AGENT_NAME, normalize_agent_name
from .path_policy import validate_config_path, validate_knowledge_root, validate_runtime_paths


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
    turn_seconds: float = 100.0
    queue_wait_seconds: float = 120.0
    local_seconds: float = 5.0
    summary_max_bytes: int = 4096
    low_watermark: float = 0.70


@dataclass(frozen=True)
class AdapterConfig:
    model: str
    base_url: str
    budgets: dict[str, Budget]
    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    verbosity: str = "high"

    def __post_init__(self):
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


def _keys(data, allowed):
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise ValueError("unknown configuration field")


def snowflake(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 20
            or any(c not in "0123456789" for c in value)
            or not 0 < int(value) < 2**64):
        raise ValueError("invalid Discord snowflake ID")
    return str(int(value))


def load_config(path: Path) -> Config:
    path = validate_config_path(path)
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    _keys(raw, {"name", "state_dir", "scratch_dir", "knowledge_dir", "discord", "runtime", "adapter", "knowledge", "pdf"})
    d = raw["discord"]
    r = raw.get("runtime", {})
    a = raw["adapter"]
    k = raw.get("knowledge", {})
    p = raw.get("pdf", {})
    _keys(d, {"guild_id", "channel_ids", "queue_capacity", "delivery_seconds"})
    _keys(r, RuntimeConfig.__dataclass_fields__)
    _keys(a, AdapterConfig.__dataclass_fields__)
    _keys(k, KnowledgeConfig.__dataclass_fields__)
    _keys(p, PdfConfig.__dataclass_fields__)
    if not isinstance(d.get("guild_id"), str):
        raise ValueError("guild_id must be a numeric string")
    guild_id = snowflake(d["guild_id"]) if d["guild_id"] else ""
    channels = d.get("channel_ids", [])
    if not isinstance(channels, list):
        raise ValueError("channel_ids must be numeric strings")
    dc = DiscordConfig(**{**d, "guild_id": guild_id, "channel_ids": tuple(snowflake(x) for x in channels)})
    rc = RuntimeConfig(**r)
    kc = KnowledgeConfig(**k)
    pc = PdfConfig(**p)
    if type(dc.queue_capacity) is not int or not 1 <= dc.queue_capacity <= 256:
        raise ValueError("invalid queue capacity")
    for seconds in (dc.delivery_seconds, rc.turn_seconds, rc.queue_wait_seconds, rc.local_seconds, a.get("cooldown_seconds", 30)):
        if type(seconds) not in (float, int) or not math.isfinite(seconds) or not 0 < seconds <= 3600:
            raise ValueError("invalid deadline")
    if type(rc.summary_max_bytes) is not int or not 128 <= rc.summary_max_bytes <= 65536:
        raise ValueError("invalid summary bound")
    if type(rc.low_watermark) not in (float, int) or not 0 < rc.low_watermark < 1:
        raise ValueError("invalid low watermark")
    if a["model"] != "gpt-6.1-sol" or a["base_url"].rstrip("/") != "https://api.openai.com/v1":
        raise ValueError("this adaptor supports official OpenAI gpt-6.1-sol only; migrate the model explicitly")
    if type(a.get("failure_threshold", 3)) is not int or not 1 <= a.get("failure_threshold", 3) <= 20:
        raise ValueError("invalid circuit failure threshold")
    budgets = a["budgets"]
    if set(budgets) != {"label", "summary", "reply"}:
        raise ValueError("exactly label, summary and reply budgets required")
    ac = AdapterConfig(**{**a, "budgets": {k: Budget(**v) for k, v in budgets.items()}})
    name = raw.get("name", AGENT_NAME)
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("invalid agent name")
    name = normalize_agent_name(name)
    def directory(key):
        value = Path(raw.get(key, key.removesuffix("_dir")))
        return path.parent / value
    config = Config(name, directory("state_dir"), directory("scratch_dir"), directory("knowledge_dir"), dc, rc, ac, kc, pc)
    validate_runtime_paths(config)
    knowledge_dir = validate_knowledge_root(config, expected=path.parent / "knowledge")
    return Config(name, Path(os.path.abspath(config.state_dir)), Path(os.path.abspath(config.scratch_dir)),
                  knowledge_dir, dc, rc, ac, kc, pc)
