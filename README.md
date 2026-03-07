# DeadReckoning

> Navigate any codebase. Dead reckoning — finding your way through unknown territory.

`dead-reckoning` parses any Python codebase into a **SurrealDB knowledge graph** — files, functions, classes, imports, and call relationships all become queryable nodes and edges. A **LangGraph agent** navigates the graph to answer architecture questions in plain English. Ingestion is **checkpointed** — kill it mid-run, restart, and it resumes exactly where it stopped.

Built at the LangChain × SurrealDB London Hackathon, March 2025.

---

## What it does

1. **Ingest** — point it at a Python repo, it walks every `.py` file and builds a knowledge graph in SurrealDB
2. **Persist** — ingestion checkpoints after every file using `langgraph-checkpoint-surrealdb`; crash-safe and resumable
3. **Query** — a LangGraph agent with graph-traversal tools answers questions about the codebase
4. **Visualise** — Streamlit UI shows the live graph and a chat interface

### The demo moment

```
Agent is indexing. Kill it at file 47 of 100.
Restart with the same thread_id.
It resumes from file 47 — not file 1.
The 46 nodes already built are still in the graph.
```

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Graph + vector DB | SurrealDB (cloud) | Graph traversal AND vector search in one DB |
| Agent orchestration | LangGraph | Stateful agent loop with native checkpointing |
| Checkpointer | langgraph-checkpoint-surrealdb | Persists agent state to SurrealDB |
| LLM (dev) | Ollama gemma3:4b | Local, fast iteration, no API cost |
| LLM (prod) | Ollama gemma3:27b | Higher quality responses for demo |
| Embeddings | Ollama nomic-embed-text | Local embeddings, no API cost |
| Code parsing | Python `ast` module | Built-in, no deps, reliable for Python |
| UI | Streamlit + streamlit-agraph | Fast to build, graph viz included |
| Observability | LangSmith | Auto-traces every agent step |

---

## Quickstart

**Prerequisites:**
- [uv](https://docs.astral.sh/uv/) — Python package manager
- [Ollama](https://ollama.com) — running locally
- [SurrealDB Cloud](https://surrealdb.com/cloud) — free instance (or self-hosted)
- [LangSmith](https://smith.langchain.com) — API key for tracing

```bash
# 1. Clone and install
git clone https://github.com/YOUR_USERNAME/dead-reckoning
cd dead-reckoning
uv venv
uv sync

# 2. Pull Ollama models
ollama pull llama3.2:3b       # LLM for dev/testing
ollama pull nomic-embed-text # embeddings

# 3. Configure environment
cp .env.example .env
# Edit .env — fill in SURREALDB_URL, SURREALDB_USER, SURREALDB_PASS, LANGCHAIN_API_KEY

# 4. Apply SurrealDB schema (one-time setup)
uv run python ingestion/apply_schema.py

# 5. Ingest a repo (replace with a real path to a Python repo)
uv run python ingestion/seed.py --repo /path/to/your/python/repo

# Example: index this repo itself
uv run python ingestion/seed.py --repo .

# 6. Run the UI
uv run streamlit run ui/app.py
```

**To demo interrupt/resume:**
```bash
# Start ingestion, kill it partway through (Ctrl-C), then re-run the same command.
# It resumes from the last checkpoint — already-processed files are skipped.
uv run python ingestion/seed.py --repo /path/to/your/python/repo
```

---

## Demo reset

Wipes all data, reapplies schema, and re-ingests the demo repo in one command:

```bash
# Clone the demo repo (httpx) if you haven't already
git clone https://github.com/encode/httpx /tmp/demo-repo

# Full reset + ingest (run this before any live demo)
uv run python demo/seed_demo.py
# Demo ready. Files: 34 | Functions: 287 | Classes: 42
```

See [`demo/DEMO_SCRIPT.md`](./demo/DEMO_SCRIPT.md) for the exact live judging script.

---

## Repo structure

```
dead-reckoning/
├── ingestion/
│   ├── parser.py          # AST extraction: files, functions, classes, imports
│   ├── loader.py          # Upsert entities + edges into SurrealDB
│   ├── schema.surql       # SurrealDB table + index definitions
│   └── seed.py            # CLI: walk a repo, checkpoint after each file
├── agent/
│   ├── state.py           # AgentState TypedDict
│   ├── tools.py           # get_dependencies, find_callers, semantic_search
│   └── graph.py           # LangGraph StateGraph + checkpointer wiring
├── ui/
│   └── app.py             # Streamlit: graph viz + chat interface
├── demo/
│   ├── seed_demo.py       # Pre-index the demo repo cleanly
│   └── DEMO_SCRIPT.md     # Exact steps + narration for live judging
├── tests/
│   ├── test_parser.py
│   ├── test_loader.py
│   └── test_agent.py
├── .env.example
├── pyproject.toml
├── ARCHITECTURE.md        # Schema, integration points, design decisions
├── DEVELOPMENT.md         # Ordered build tasks with success criteria
└── README.md
```

---

## Documentation

| Doc | Purpose |
|---|---|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Schema design, integration points, test criteria |
| [DEVELOPMENT.md](./DEVELOPMENT.md) | Ordered build tasks — start here if contributing |
| [demo/DEMO_SCRIPT.md](./demo/DEMO_SCRIPT.md) | Live demo script: exactly what to type and say |

---

## Environment variables

see .env.example

---

## Built with

- [SurrealDB](https://surrealdb.com) — multi-model database powering the knowledge graph
- [LangGraph](https://langchain-ai.github.io/langgraph/) — agent orchestration and state management
- [langgraph-checkpoint-surrealdb](https://github.com/TODO) — SurrealDB checkpointer for LangGraph
- [LangChain](https://langchain.com) — LLM tooling and observability via LangSmith

---

*LangChain × SurrealDB London Hackathon — March 2026*
