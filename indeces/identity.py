"""Current product name and compatibility aliases for existing local state."""

AGENT_NAME = "Indeces"
SELF_NAME_ALIASES = frozenset({"indeces", "indices"})


def normalize_agent_name(name: str) -> str:
    """Upgrade the previous product name without changing a custom persona."""
    return AGENT_NAME if name.strip().casefold() in SELF_NAME_ALIASES else name
