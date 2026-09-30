from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Budget:
    input_tokens: int
    output_tokens: int
    seconds: float
    reasoning: str = "none"

    def __post_init__(self):
        if type(self.input_tokens) is not int or not 1 <= self.input_tokens <= 1_050_000:
            raise ValueError("invalid input_tokens")
        if type(self.output_tokens) is not int or not 1 <= self.output_tokens <= 128_000:
            raise ValueError("invalid output_tokens")
        if self.input_tokens + self.output_tokens > 1_050_000:
            raise ValueError("input plus output exceeds model context")
        if type(self.seconds) not in (float, int) or not math.isfinite(self.seconds) or not 0 < self.seconds <= 300:
            raise ValueError("invalid stage deadline")
        if self.reasoning not in {"none", "low", "medium", "high", "xhigh", "max"}:
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


@dataclass(frozen=True)
class KnowledgeConfig:
    poll_seconds: float = 0.5
    max_file_bytes: int = 8192
    max_files: int = 128
    chunk_characters: int = 400
    version_seconds: float = 360.0
    version_input_tokens: int = 65536
    version_output_tokens: int = 16384


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
    path = path.resolve()
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    _keys(raw, {"name", "state_dir", "scratch_dir", "knowledge_dir", "discord", "runtime", "adapter", "knowledge"})
    d = raw["discord"]
    r = raw.get("runtime", {})
    a = raw["adapter"]
    k = raw.get("knowledge", {})
    _keys(d, {"guild_id", "channel_ids", "queue_capacity", "delivery_seconds"})
    _keys(r, RuntimeConfig.__dataclass_fields__)
    _keys(a, AdapterConfig.__dataclass_fields__)
    _keys(k, KnowledgeConfig.__dataclass_fields__)
    if not isinstance(d.get("guild_id"), str):
        raise ValueError("guild_id must be a numeric string")
    guild_id = snowflake(d["guild_id"]) if d["guild_id"] else ""
    channels = d.get("channel_ids", [])
    if not isinstance(channels, list):
        raise ValueError("channel_ids must be numeric strings")
    dc = DiscordConfig(**{**d, "guild_id": guild_id, "channel_ids": tuple(snowflake(x) for x in channels)})
    rc = RuntimeConfig(**r)
    kc = KnowledgeConfig(**k)
    if type(dc.queue_capacity) is not int or not 1 <= dc.queue_capacity <= 256:
        raise ValueError("invalid queue capacity")
    for seconds in (dc.delivery_seconds, rc.turn_seconds, rc.queue_wait_seconds, rc.local_seconds, a.get("cooldown_seconds", 30)):
        if type(seconds) not in (float, int) or not math.isfinite(seconds) or not 0 < seconds <= 3600:
            raise ValueError("invalid deadline")
    if type(rc.summary_max_bytes) is not int or not 128 <= rc.summary_max_bytes <= 65536:
        raise ValueError("invalid summary bound")
    if type(rc.low_watermark) not in (float, int) or not 0 < rc.low_watermark < 1:
        raise ValueError("invalid low watermark")
    for field in (kc.poll_seconds, kc.version_seconds):
        if type(field) not in (int, float) or not math.isfinite(field) or not 0 < field <= 3600:
            raise ValueError("invalid knowledge deadline")
    for field in (kc.max_file_bytes, kc.max_files, kc.chunk_characters, kc.version_input_tokens, kc.version_output_tokens):
        if type(field) is not int or field <= 0:
            raise ValueError("invalid knowledge bounds")
    if kc.max_file_bytes > 1024 * 1024 or kc.max_files > 1024 or kc.chunk_characters > 4000:
        raise ValueError("knowledge bounds exceed small-runtime contract")
    if a["model"] != "gpt-6-luna" or a["base_url"].rstrip("/") != "https://api.openai.com/v1":
        raise ValueError("this adaptor supports official OpenAI gpt-6-luna only")
    if type(a.get("failure_threshold", 3)) is not int or not 1 <= a.get("failure_threshold", 3) <= 20:
        raise ValueError("invalid circuit failure threshold")
    budgets = a["budgets"]
    if set(budgets) != {"label", "summary", "reply"}:
        raise ValueError("exactly label, summary and reply budgets required")
    ac = AdapterConfig(**{**a, "budgets": {k: Budget(**v) for k, v in budgets.items()}})
    name = raw.get("name", "Indices")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("invalid agent name")
    def directory(key):
        value = Path(raw.get(key, key.removesuffix("_dir")))
        return (path.parent / value).resolve()
    return Config(name, directory("state_dir"), directory("scratch_dir"), directory("knowledge_dir"), dc, rc, ac, kc)
