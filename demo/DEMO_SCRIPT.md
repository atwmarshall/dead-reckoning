# Demo Script — DeadReckoning

Live judging — Sunday ~14:00. You have **2 minutes**. This is the exact script.

Rehearse it 3 times Saturday evening. Know every click cold. Have all tabs open and ready before the judges arrive at your station.

---

## Setup (before judges arrive)

### What `seed_demo.py` does

1. `--httpx` — clones encode/httpx, ingests it into a SurrealDB knowledge graph (big, impressive graph)
2. (no flag) — ingests v1 sample fixture with ingestion records, content hashes, and tar snapshots
3. `--with-v2` — ingests v1 + v2 fixtures + computes diff (for testing the full pipeline)
4. `--reset-only` — wipes all data and re-applies schema

### Demo setup commands

```bash
# Saturday evening — test the full pipeline end-to-end
uv run python demo/seed_demo.py --with-v2
uv run pytest tests/test_tools.py -v

# Sunday morning — set up both graphs for the demo
# Step 1: Reset + ingest httpx (the "wow" graph)
uv run python demo/seed_demo.py --httpx

# Step 2: Add v1 fixture ON TOP (--no-reset keeps httpx data intact)
uv run python demo/seed_demo.py --no-reset

# Step 3: Open the app — select httpx ingestion in sidebar, leave on Knowledge Graph tab
uv run streamlit run ui/app.py
```

### Tabs to have open

```
Browser tab 1: Streamlit app (localhost:8501) — Knowledge Graph tab, httpx graph showing
Browser tab 2: LangSmith (smith.langchain.com) — dead-reckoning project
Terminal: open, minimised, ready
```

### State before judges arrive

- httpx fully ingested — graph shows hundreds of nodes and edges (files, functions, classes, calls)
- v1 sample fixture also ingested — ready for the diff moment later
- v1 ingestion was interrupted mid-run and resumed — both runs visible in LangSmith
- v2 sample fixture NOT yet ingested — waiting to trigger live

### Demo flow

1. **Open** — show httpx graph (impressive size, real-world repo)
2. **Interrupt/resume** — point at LangSmith traces
3. **Switch to fixture** — ingest v2 live, show diff colours
4. **Query** — ask the agent, show tool chain
5. **LangSmith** — show the reasoning trace
6. **Close** — back to graph

---

## The script

---

**[0:00 — OPEN with the problem + httpx graph]**

*Show graph tab — httpx fully indexed, hundreds of nodes*

Say:
> "Every developer knows this moment. New codebase, no idea what talks to what. You're reading files trying to reconstruct a map that already exists — it's just locked in the code."

*Pan around the httpx graph — point out files, functions, call edges*

Say:
> "This is encode/httpx — a real Python HTTP library, 30+ files, hundreds of functions. We pointed our agent at the GitHub URL and it parsed every file into a SurrealDB knowledge graph. Files, functions, classes, call relationships — all queryable nodes and edges."

---

**[0:15 — FIRST WOW — interrupt and resume]**

*Click to LangSmith tab — show the interrupted run, then the resumed run below it*

Say:
> "Watch the ingestion pipeline — each step traced, each file counted. Halfway through — I killed it. Here's the LangSmith trace."

*Point at the two runs — interrupted, then resumed*

Say:
> "Same thread ID. It resumed from checkpoint — exactly where it stopped. The nodes already built stayed in the graph. That's the SurrealDB checkpointer: agent state survives the kill."

*Switch back to Streamlit*

---

**[0:35 — SECOND WOW — versioned diff]**

*In the sidebar, switch to the v1 fixture ingestion to show it in the graph. Then click "v2 — sample repo (with changes)" quick-select → path fills.*

Say:
> "Now let's show what happens when code changes. Here's a small repo — version one is already indexed."

*Click Ingest → conflict dialog: "A previous version exists"*

Say:
> "It detects the previous version and asks what to do."

*Click "Add new version" → graph animates → nodes turn green, yellow, red*

Say:
> "Green: unchanged. Yellow: modified. Red: deleted. Not just files — individual functions are green, yellow, or red. The knowledge graph is now a diff."

*Open terminal. Type:*
```bash
tar tf ~/.dead-reckoning/snapshots/$(ls -t ~/.dead-reckoning/snapshots/ | head -1)
```

Say:
> "This is a real tar file — content-addressed, same principle Docker uses for image layers. SHA-256 per file, compared across versions. Pure Python stdlib, zero extra dependencies."

---

**[1:05 — QUERY — three tools, two chained together]**

*Switch to "Ask the Codebase" tab. Type:*
```
what changed between versions and what might be affected?
```

Say (while agent responds):
> "Watch the agent reason. It has four tools — and right now it's chaining two of them together. First it calls **version_diff** — that auto-detects the versions from the ingestion table, then reads the diff_status field from every file and function node in SurrealDB. Red, yellow, green. Then it chains into **trace_impact** on the modified files — that's a multi-hop graph traversal: 'what calls this function, and what calls *that*?' Two hops through the calls graph in a single SurrealQL query. Not possible in a vector database. That two-tool reasoning chain is a LangGraph conditional edge — visible right now in LangSmith."

*Point at tool calls appearing in the response*

Say:
> "And the search itself — **hybrid_search** — uses `search::rrf()`. One SurrealQL statement fusing BM25 keyword matching with HNSW vector similarity. Reciprocal Rank Fusion computed entirely inside SurrealDB. No application-side stitching."

---

**[1:25 — LANGSMITH — show the reasoning chain]**

*Switch to LangSmith — trace for the query visible*

Say:
> "Every operation observable. The agent called version_diff, saw the yellow files, then called trace_impact to find downstream callers. A two-tool reasoning chain — visible right here in the trace."

*Point at the tool call chain — show version_diff → trace_impact sequence*

Say:
> "Both the ingestion agent and the query agent are fully traced. Tool calls, graph traversals, fully auditable."

---

**[1:45 — CLOSE]**

*Switch back to graph — coloured nodes*

Say:
> "A knowledge graph that survives crashes, versions itself like a container layer, and lets you query across the diff. Four tools — semantic search with native RRF, multi-hop impact analysis, version diffing, and ingestion history. All backed by SurrealDB — the graph, the vectors, the checkpoints, the version history. Same database, four jobs. LangGraph routes the agent through conditional tool chains, and LangSmith traces every step."

---

**[2:00 — DONE]**

---

## What makes each tool special (for judge Q&A)

### hybrid_search
- **What it does:** Finds functions by concept or name using combined semantic + keyword search
- **SurrealDB feature:** `search::rrf()` fuses HNSW vector similarity and BM25 full-text matching in a single SurrealQL query — no application-side rank merging
- **Graph enrichment:** Results are enriched with parent class and sibling functions via graph traversal
- **LangSmith:** Nested spans show vector search, keyword search, RRF fusion, and per-result graph enrichment
- **Prompt to trigger (httpx):** `"which functions handle authentication?"` or `"find the HTTP client logic"`
- **Prompt to trigger (fixtures):** `"find the slugify function"` or `"database configuration settings"`

### trace_impact
- **What it does:** Maps the blast radius of a change — finds everything that directly or transitively depends on a function
- **SurrealDB feature:** Multi-hop graph traversal (`<-calls<-function<-calls<-function`) in a single query — 2 hops through the calls graph, returning direct callers AND their callers
- **Why it matters:** This is structural analysis that context windows can't do. "What calls X, and what calls that?" requires the graph
- **LangSmith:** Single retriever span showing the multi-hop SurrealQL query
- **Prompt to trigger (httpx):** `"what would break if I changed _send?"` or `"what depends on _client?"`
- **Prompt to trigger (fixtures):** `"what would break if I changed slugify?"` or `"what depends on utils?"`

### version_diff
- **What it does:** Summarises what changed between two ingested versions at file AND function granularity
- **SurrealDB feature:** Auto-detects versions from the `ingestion` table, then reads `diff_status` from the versioned knowledge graph, traverses `->contains->function` edges to show per-function changes within each file
- **Why it matters:** The graph is version-aware — not just "what exists" but "what changed" — and the agent can reason over the diff. Zero arguments needed — it figures out what to compare
- **LangSmith:** Shows ingestion table query + diff_status graph queries as nested spans
- **Prompt to trigger:** `"what changed between versions?"` or `"show me the diff summary"`

### list_versions
- **What it does:** Shows all ingested repositories, their versions, file counts, timestamps, and snapshot status
- **SurrealDB feature:** Queries the `ingestion` table directly — demonstrates SurrealDB as a structured metadata store alongside its graph/vector roles
- **Why it matters:** The agent can answer "what's been indexed?" without the user needing to check the UI. Lightweight — doesn't trigger any diff computation
- **LangSmith:** Single retriever span querying the ingestion table
- **Prompt to trigger:** `"what repos are indexed?"` or `"what versions are available?"` or `"what has been ingested?"`

### Multi-tool chain (the demo moment)
- The prompt `"what changed between versions and what might be affected?"` triggers **version_diff** first, then the agent chains into **trace_impact** on the modified items
- This shows LangGraph's conditional tool routing: the agent reasons about the diff, then decides to assess the blast radius
- Visible as a two-step tool call sequence in LangSmith — the key demo of agent reasoning over a knowledge graph

---

## Pre-typed queries — scratch file, copy-paste during demo

```
# After v1→v2 diff (fixtures)
what changed between versions and what might be affected?

# On httpx graph (impressive for judges)
which functions handle authentication?
what would break if I changed _send?
find the HTTP client logic and explain how requests flow

# Show ingestion awareness (any graph)
what repos have been indexed and how many versions?
```

---

## Anticipated judge questions

**"How is this different from just using an LLM with the code as context?"**
> "Context windows can't do multi-hop graph traversal. 'What calls this function, and what calls that?' is structural — you need the graph. And our search uses SurrealDB's native `search::rrf()` to fuse vector and keyword results inside the database, not in Python. LLMs also forget between sessions. Our agent accumulates knowledge across runs and versions."

**"Why tar-based snapshots?"**
> "Same content-addressing principle as Docker image layers — SHA-256 per file, compared across versions. Pure Python stdlib (`tarfile` + `hashlib`), zero extra dependencies. Deletions are detected by set difference: if a file was in the old snapshot but not the new one, it's red."

**"How granular is the diff?"**
> "Function-level. We hash each function's source text and compare across versions. A modified file gets yellow, but inside it individual functions show green, yellow, or red — so you can see exactly which functions changed."

**"What happens to the graph on a new version?"**
> "We diff the two tar snapshots — old SHA-256 vs new. Same hash goes green, changed goes yellow, absent goes red. SurrealDB nodes get a diff_status field updated in place. No re-ingestion needed for the diff — it's pure snapshot comparison."

**"Why SurrealDB specifically?"**
> "One instance doing four things: knowledge graph with typed edges, HNSW vector search with BM25 full-text via native `search::rrf()`, LangGraph checkpoint state, and ingestion history with version tracking. Graph traversal AND vector search AND relational queries in the same SurrealQL statement. No second database anywhere."

**"What's LangGraph doing here?"**
> "Two things. First, the query agent is a LangGraph StateGraph with conditional edges — the LLM decides which tools to call, and chains them together. version_diff into trace_impact is a two-step reasoning chain, not hardcoded. Second, the ingestion pipeline is a separate LangGraph graph with per-file checkpoints — kill it, resume it, same thread ID. Both share the same SurrealDB checkpointer via langgraph-checkpoint-surrealdb."

**"What does LangSmith show?"**
> "Every agent step is observable. Tool calls appear as nested spans — you can see the LLM reasoning, the SurrealQL queries, the graph traversals. The multi-tool chain (version_diff then trace_impact) is visible as a two-step sequence. Both the ingestion agent and query agent are fully traced. We use `@traceable` decorators on every retrieval function."

**"What's search::rrf()?"**
> "Reciprocal Rank Fusion. We run two searches — HNSW vector similarity for semantic meaning and BM25 for keyword matching — then SurrealDB's built-in `search::rrf()` function merges both ranked lists into one. The fusion happens inside the database in a single query, not in our Python code."

**"Does it work on non-Python repos?"**
> "Python only for the AST parsing — we used the built-in ast module. tree-sitter adds multi-language support; that's the obvious next step. The snapshot and diff layer works on any file type already."

**"Why httpx?"**
> "It's a well-known, well-structured Python HTTP library — 30+ files, hundreds of functions, real class hierarchies and call chains. Judges can recognise the code. It's big enough to show the graph is useful, small enough to ingest in under a minute."

---

## Timing failsafes

**If diff colouring is slow (> 5s):** Keep talking — "computing SHA-256 across both tar snapshots, comparing every file..." — it will arrive.

**If the conflict dialog doesn't appear:** v1 ingestion_id not in session state. Refresh, re-ingest v1 quickly (fixture repo is fast), then ingest v2.

**If the agent query is slow (> 15s):** Switch to LangSmith immediately — "you can watch it reasoning right now" — point at tool calls firing. The wait becomes part of the demo.

**If you need to show interrupt/resume live instead of via LangSmith history:**
```bash
python -c "
from agent.ingest_graph import build_ingestion_agent
agent = build_ingestion_agent()
config = {'configurable': {'thread_id': 'ingest-sample-v1'}}
agent.invoke(None, config)
"
```

**If Streamlit crashes entirely:**
```bash
uv run python -c "
from agent.graph import build_query_agent
agent = build_query_agent()
config = {'configurable': {'thread_id': 'demo-fallback'}}
r = agent.invoke({
    'messages': [('user', 'what changed between versions and what might be affected?')],
}, config)
print(r['messages'][-1].content)
"
```

**If you have under 90 seconds:** Skip the diff and query. Do: httpx graph (pan around, "hundreds of nodes") → interrupt/resume (point at LangSmith history) → close with "three tools, one database". The visual graph and the resume moment are enough.

**The two things that must not fail:**
1. httpx graph rendering — impressive node count, visible call edges
2. Green/yellow/red colouring on the v1→v2 fixture diff

Rehearse both flows until they work perfectly every single time before Sunday morning.
