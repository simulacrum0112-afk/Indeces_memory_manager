# Claude provider

Console `model` selects independent OpenAI or official Anthropic profiles. The
Anthropic profile supports Claude Opus 5.5 and Fable 5.1 at the official endpoint,
with explicit high effort and the existing per-stage token/time caps. Omitted
provider settings retain OpenAI behavior. `config.claude.example.toml` contains
public settings only; credentials remain in environment references or separately
encrypted Windows vaults and are entered by the user through `apikey`.

The ordinary factory shares one native adapter with Runtime, selection and
KnowledgeService. It uses native Messages token counting, one generation,
structured local validation, native usage provenance and known/unknown failure
handling. It adds no automatic retry, fallback, service start or hot reload.
Richard concept CIDs remain local audit metadata.

Claude controlled-session, active/path requery and paid bounded-reingestion
activation are rejected before credentials or service resources are prepared.
The separate explicit tool add-on and its limits are documented in
[CLAUDE_PRUNE.md](CLAUDE_PRUNE.md). Ordinary provider selection does not grant
permission for paid calls or migrate financial state.

The production-derived provider source passed its offline acceptance and a
separately authorized native text smoke. A source-only deployment preserved user
configuration, credentials, database, corpus and existing production Git state;
no service was started. Public release uses the existing main history and keeps
unrelated files. Installation and live Console/Discord behavior remain separate
from the native smoke and isolated tool-loop checks.
