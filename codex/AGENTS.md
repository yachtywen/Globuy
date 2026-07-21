# globuy canonical workspace instructions

These instructions apply to the entire `globuy` repository. The root `AGENTS.md` is intentionally
kept as a discovery bootstrap because repository-scoped Codex instructions are discovered from the
workspace hierarchy; this file contains the canonical detailed rules.

## Required reading order

Before doing any project work:

1. Read `.codex/AGENTS.md` completely.
2. Read `.codex/codex.md` completely.
3. Read `docs/project-status.md` for current implementation truth.
4. Read `docs/vector-infrastructure.md` before any recall, memory, RAG, embedding, ANN, search,
   OpenSearch, Faiss, Milvus, Qdrant, Redis, or vector-store work.
5. Read relevant items in `docs/ToDo.md` for near-term learning plans.
6. Inspect the real code before explaining, planning, or changing behavior.

## Mandatory behavior

- Treat user-provided reference screenshots as target contracts, not proof of current implementation.
- Translate the reference name `globex` to this repository's name `globuy` in all new code and docs.
- Keep target design, current implementation, and known gaps explicitly separated.
- Unconfigured external tools must return `not_configured`; never fabricate products, prices,
  inventory, sources, shipping, duty, or search results.
- Tests must not call a real paid model or real shopping provider.
- Use Conda `globuy` with Python 3.12; do not recreate a project `.venv`.
- Never expose or document `.env` secrets.
- Streaming is mandatory: new AgentLoop/API behavior must use incremental events suitable for AG-UI,
  instead of being designed around one blocking final response.
- Vector choices in `docs/vector-infrastructure.md` are fixed. Do not replace Faiss, OpenSearch,
  LangGraph BaseStore, the three-tower Query encoder, or their metrics without user approval.

## Automatic documentation maintenance

After any meaningful implementation, architecture, dependency, configuration, test, scope, or
reference-contract change, update `docs/project-status.md` in the same turn without a reminder.
This includes modules, AgentLoop/fork/state/phases/tools/prompts/events, external infrastructure,
dependencies, examples/tests, new user reference material, blockers, gaps, and priorities.

When updating status documentation:

1. Update the last-updated date.
2. State current implementation and remaining gaps, not only completed work.
3. Record only verified commands/results; label unverified claims.
4. Append a change-log entry without erasing important decisions.
5. Never include secrets.
6. Update `README.md` when startup or user-facing behavior changes.
7. Update `docs/ToDo.md` only when short-term learning tasks change.

Pure explanation with no project-fact change does not require a meaningless status edit.

## Completion checklist

Before declaring a feature complete, verify its Think/Reflect/Act phase, distinguish main-loop tools
from homogeneous forks and heterogeneous experts, define state/API/event contracts, obey the vector
contract, expose stream/errors, run relevant tests without paid calls, and update project status.

