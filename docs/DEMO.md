# Demo walkthrough (3 minutes)

A scripted end-to-end tour you can run locally. Uses the included `tests/fixtures/sample_repo/v1` and `v2` so it works offline, no external repo required.

> **Model note:** the default is now `gemma4:e2b` (~7 GB) — recent enough to produce clean structured tool calls and small enough to run on a laptop. Older small models such as `llama3.2:3b` will work most of the time but can occasionally emit tool calls as plain text or with malformed arguments, which trips up the multi-tool chain in step 4. If you hit that, fall back to `gpt-oss:20b`.

## 1. Seed the v1 fixture

```bash
uv run python demo/seed_demo.py
uv run streamlit run ui/app.py
```

Open `http://localhost:8501`. The **Knowledge Graph** tab shows `18 nodes · 25 edges` — files, functions, classes, and `calls` / `contains` / `in_repo` / `imports` edges from the v1 fixture.

## 2. Query 1 — hybrid search + graph traversal

Click the **Ask the Codebase** tab and send:

```
tell me about the slugify function
```

The agent runs `hybrid_search` (HNSW vector + BM25 keyword fused with `search::rrf()`), then chains into `trace_impact` to walk `<-calls<-function` edges. You get back:

- **Location:** `tests/fixtures/sample_repo/v1/utils.py`
- **Direct callers:** `display_items` (in `main.py`)
- **Transitive callers:** `run` (in `main.py` calls `display_items`, which calls `slugify`)

The right-hand **Context Graph** panel shows the retrieved nodes.

## 3. Ingest v2 live — checkpoint + resume

In the sidebar, open **Quick select** -> click **v2 — sample repo (with changes)** -> click **Ingest**.

A **"Repo already ingested"** dialog appears. Click **Add new version**.

The ingestion pipeline parses v2, creates a snapshot, computes the diff against v1, and then **pauses at a LangGraph interrupt** — the sidebar shows *"Diff ready — review the graph, then click Resume"* with a **Resume** button.

This is SurrealDB's checkpointer in action: the ingestion agent's state is persisted. Kill the process, come back, and it resumes from the same point.

Click **Resume**. v2 files stream in, call edges are rebuilt, the knowledge graph updates to `34 nodes · 47 edges` with diff colours: **red** (deleted), **yellow** (modified), **purple** (new), **green** (unchanged).

## 4. Query 2 — the hero multi-tool chain

Back on **Ask the Codebase**, send:

```
what changed between versions - any issues? and action them
```

The agent autonomously chains three tools:

1. **`version_diff`** — reads `diff_status` from the versioned graph. Reports deleted `models.py`, modified `utils.py`, new undocumented `Item.__repr__`.
2. **`generate_docstring`** — reads the function source from SurrealDB, sends it to the LLM, returns a Python docstring.
3. **`raise_issue`** — opens a real GitHub issue via `gh issue create` with the suggestion.

Expected output ends with a clickable **"Issue: Add missing docstring for Item.__repr__"** link.

## 5. Inspect the LangSmith trace

Open your LangSmith project (default: `dead-reckoning`). The most recent trace shows the full waterfall:

```
LangGraph
├── llm (ChatOllama gemma4:e2b)
├── tools -> version_diff
├── llm (ChatOllama gemma4:e2b)
├── tools -> hybrid_search
│   ├── embed_query      (Ollama embedding call)
│   ├── rrf_retrieve     (SurrealDB HNSW + BM25 + search::rrf)
│   └── graph_enrich     (parent class + siblings + calls-edge traversal)
├── tools -> generate_docstring
└── tools -> raise_issue
```

Every retrieval stage, every LLM call, and the full reasoning chain is visible as nested spans. This is the observability surface for debugging hybrid search quality: when a query returns the wrong answer, you open the trace and identify which stage failed.

## Troubleshooting

- **Agent emits tool calls as plain text** (e.g., `{"name": "slugify", "parameters": {}}`) — your model isn't producing structured tool calls. Switch to `gpt-oss:20b` or another tool-use-capable model.
- **Resume button doesn't appear during v2 ingestion** — pull the latest `main`; this was a race condition fixed in [#22](https://github.com/atwmarshall/dead-reckoning/pull/22).
- **`calls` count is 0 after seeding** — pull the latest `main`; `load_calls` had a silent failure fixed in [#18](https://github.com/atwmarshall/dead-reckoning/pull/18).
- **`gh issue create` fails** — run `gh auth login` first.
