"""Explicit isolated offline add-on; the ordinary provider factory is unchanged."""
from .contracts import GovernedError
from .model_provider import create_model_adapter


def create_offline_prune_loop(adapter_config, scratch, budget, *, graph_dir,
                             indeces_dir, question_id, seeds_A, seeds_B,
                             target, level="coarse", request_override,
                             log_path=None, max_rounds=8):
    """Build the real frozen Graph/Session and native Claude seam without keys.

    This first integration entry is deliberately offline. Live activation uses
    the same original grant and requires separately verified financial binding.
    The existing OpenAI and text-only Claude factory remains available in parallel.
    """
    if not callable(request_override):
        raise GovernedError("prune_live_not_authorized", remote_usage_unknown=False)
    if (adapter_config.provider != "anthropic"
            or adapter_config.model != "claude-opus-5-5"
            or adapter_config.base_url.rstrip("/") != "https://api.anthropic.com/v1"):
        raise GovernedError("invalid_prune_provider", remote_usage_unknown=False)
    from ._vendor.npmi_prune import Graph, Session
    from .claude_prune_loop import PruneToolLoop
    graph = Graph(str(graph_dir), str(indeces_dir), verify_hashes=True)
    session = Session(graph, question_id, seeds_A, seeds_B, target,
                      level=level, log_path=log_path, text_provider=None)
    adapter = create_model_adapter(adapter_config, scratch, "")
    adapter._request_override = request_override
    loop = PruneToolLoop(adapter, session, budget, max_rounds=max_rounds)
    return loop, adapter
