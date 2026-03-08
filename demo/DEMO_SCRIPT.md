# Demo Script — DeadReckoning

Live judging — Sunday ~14:00. You have **2 minutes**. This is the exact script.

Rehearse it 3 times Saturday evening. Know every click cold. Have all tabs open and ready before the judges arrive at your station.

---

## Setup (before judges arrive)

### Prerequisites

- SurrealDB Cloud instance running (check surreal.cloud dashboard)
- Ollama running locally with models pulled
- `.env` populated (copy from `.env.example`)
- LangSmith project `dead-reckoning` exists

### 1. Verify Ollama models are pulled

```bash
ollama list
# Must see: llama3.2:3b (or your demo model) + nomic-embed-text
# If missing:
ollama pull llama3.2:3b
ollama pull nomic-embed-text
```

### 2. Verify SurrealDB connection

```bash
# Quick connection test — should return empty array, not an error
uv run python -c "
import asyncio, os
from dotenv import load_dotenv
from surrealdb import AsyncSurreal
load_dotenv()
async def check():
    db = AsyncSurreal(os.environ['SURREALDB_URL'])
    await db.connect()
    await db.signin({'username': os.environ['SURREALDB_USER'], 'password': os.environ['SURREALDB_PASS']})
    await db.use(os.environ['SURREALDB_NS'], os.environ['SURREALDB_DB'])
    print(await db.query('SELECT count() FROM file GROUP ALL'))
    await db.close()
asyncio.run(check())
"
```

### 3. Seed the demo data

```bash
# Saturday evening — test the full pipeline end-to-end
uv run python demo/seed_demo.py --with-v2
uv run pytest tests/test_tools.py -v

# Sunday morning — set up for the live demo
# Step 1: Reset + ingest httpx (the "wow" graph)
uv run python demo/seed_demo.py --httpx

# Step 2: Add v1 fixture ON TOP (--no-reset keeps httpx data intact)
uv run python demo/seed_demo.py --no-reset

# Step 3: Verify counts
uv run python -c "
import asyncio, os
from dotenv import load_dotenv
from surrealdb import AsyncSurreal
load_dotenv()
async def counts():
    db = AsyncSurreal(os.environ['SURREALDB_URL'])
    await db.connect()
    await db.signin({'username': os.environ['SURREALDB_USER'], 'password': os.environ['SURREALDB_PASS']})
    await db.use(os.environ['SURREALDB_NS'], os.environ['SURREALDB_DB'])
    for t in ['file', 'function', 'class', 'calls', 'imports', 'ingestion']:
        r = await db.query(f'SELECT count() FROM \`{t}\` GROUP ALL')
        print(f'{t:>12}: {r}')
    await db.close()
asyncio.run(counts())
"
```

### 4. Start the app

```bash
uv run streamlit run ui/app.py
```

### 5. Prepare SurrealQL queries in a scratch terminal

Have these ready to copy-paste during the demo. Open a second terminal window.

**Query 1 — Hybrid search (RRF fusion inside SurrealDB):**
```bash
uv run python -c "
import asyncio, os
from dotenv import load_dotenv
from surrealdb import AsyncSurreal
from langchain_ollama import OllamaEmbeddings
load_dotenv()

async def rrf_demo():
    embedder = OllamaEmbeddings(model='nomic-embed-text')
    vec = embedder.embed_query('authentication')

    db = AsyncSurreal(os.environ['SURREALDB_URL'])
    await db.connect()
    await db.signin({'username': os.environ['SURREALDB_USER'], 'password': os.environ['SURREALDB_PASS']})
    await db.use(os.environ['SURREALDB_NS'], os.environ['SURREALDB_DB'])

    rows = await db.query_raw('''
        LET \$vs = SELECT name, file.path AS path,
                          vector::similarity::cosine(embedding, \$vec) AS score
                   FROM function
                   WHERE embedding <|5,100|> \$vec;
        LET \$ft = SELECT name, file.path AS path,
                          search::score(0) + search::score(1) AS score
                   FROM function
                   WHERE name @0@ \$keyword OR docstring @1@ \$keyword
                   ORDER BY score DESC LIMIT 10;
        RETURN search::rrf([\$vs, \$ft], 5, 60);
    ''', {'vec': vec, 'keyword': 'auth'})

    # Extract results from query_raw response
    stmts = rows.get('result', [])
    results = stmts[-1].get('result', []) if stmts else []
    for r in results[:5]:
        print(f\"  {r.get('name', '?'):30s}  {r.get('path', '?')}\")
    await db.close()

asyncio.run(rrf_demo())
"
```

**Query 2 — Multi-hop graph traversal (blast radius):**
```bash
uv run python -c "
import asyncio, os
from dotenv import load_dotenv
from surrealdb import AsyncSurreal
load_dotenv()

async def impact():
    db = AsyncSurreal(os.environ['SURREALDB_URL'])
    await db.connect()
    await db.signin({'username': os.environ['SURREALDB_USER'], 'password': os.environ['SURREALDB_PASS']})
    await db.use(os.environ['SURREALDB_NS'], os.environ['SURREALDB_DB'])

    rows = await db.query('''
        SELECT name, file.path AS path,
               <-calls<-function.name AS direct_callers,
               <-calls<-function<-calls<-function.name AS transitive_callers
        FROM function
        WHERE name CONTAINS \"slugify\"
    ')
    print('--- Blast radius for slugify ---')
    for r in (rows if isinstance(rows, list) else []):
        print(f\"  {r.get('name')}: callers={r.get('direct_callers')}, transitive={r.get('transitive_callers')}\")
    await db.close()

asyncio.run(impact())
"
```

**Query 3 — Diff status from versioned graph:**
```bash
uv run python -c "
import asyncio, os
from dotenv import load_dotenv
from surrealdb import AsyncSurreal
load_dotenv()

async def diff():
    db = AsyncSurreal(os.environ['SURREALDB_URL'])
    await db.connect()
    await db.signin({'username': os.environ['SURREALDB_USER'], 'password': os.environ['SURREALDB_PASS']})
    await db.use(os.environ['SURREALDB_NS'], os.environ['SURREALDB_DB'])

    rows = await db.query('''
        SELECT path, diff_status,
               ->contains->function.name AS functions
        FROM file
        WHERE diff_status IS NOT NONE
        ORDER BY diff_status
    ''')
    for r in (rows if isinstance(rows, list) else []):
        status = r.get('diff_status', '?').upper()
        print(f\"  [{status:>8}] {r.get('path')} -> functions: {r.get('functions')}\")
    await db.close()

asyncio.run(diff())
"
```

### 6. Tabs to have open

```
Browser tab 1: Streamlit app (localhost:8501) — Knowledge Graph tab, httpx selected
Browser tab 2: LangSmith (smith.langchain.com) — dead-reckoning project, recent traces
Terminal 1:    Streamlit running
Terminal 2:    Ready for SurrealQL demo queries (scratch queries above)
```

### State before judges arrive

- httpx fully ingested — graph shows hundreds of nodes and edges
- v1 sample fixture also ingested — ready for the diff moment later
- v2 sample fixture NOT yet ingested — waiting to trigger live
- LangSmith has traces from seed runs visible
- SurrealQL scratch queries tested and working

---

## The script

---

**[0:00 — OPEN: problem + httpx graph]**

*Show graph tab — httpx fully indexed, hundreds of nodes. Pan slowly so judges can see the density.*

Say:
> "Every developer knows this moment — new codebase, no idea what talks to what. Dead Reckoning turns any Python repo into a queryable knowledge graph."

*Point at nodes and edges*

Say:
> "This is encode/httpx — a real Python HTTP library. We pointed our agent at the GitHub URL and it parsed every file into a SurrealDB knowledge graph. Files, functions, classes, call relationships — all stored as nodes and edges."

---

**[0:15 — SURREALDB: run a live query in the terminal]**

*Switch to Terminal 2. Run the hybrid search query (Query 1 from scratch queries above).*

Say:
> "Let me show you what SurrealDB is doing under the hood. This is a single SurrealQL statement — it runs HNSW vector search AND BM25 keyword search, then fuses them with `search::rrf()` — reciprocal rank fusion — all inside the database. No application-side stitching."

*Results appear — function names and file paths*

Say:
> "One database doing graph traversal, vector search, and full-text search. That's the foundation everything else is built on."

---

**[0:35 — VERSIONED DIFF: ingest v2 live, show colours]**

*Switch to Streamlit. In sidebar, select the v1 fixture. Then trigger v2 ingestion — click the quick-select for v2, click Ingest.*

Say:
> "Now watch what happens when code changes. Version one is already indexed — let's ingest version two."

*Conflict dialog appears: "A previous version exists"*

Say:
> "It detects the previous version automatically."

*Click "Add new version" — graph updates — nodes turn green, yellow, red*

Say:
> "Green: unchanged. Yellow: modified. Red: deleted. Not just files — individual functions get their own diff status. The knowledge graph is now version-aware."

---

**[0:55 — SURREALDB PROOF: show diff data in terminal]**

*Switch to Terminal 2. Run the diff status query (Query 3 from scratch queries).*

Say:
> "Here's the raw SurrealDB data — every file node has a `diff_status` field, and we traverse `->contains->function` edges to see which functions changed inside each file. Graph traversal AND version tracking in one query."

---

**[1:05 — AGENT: multi-tool reasoning chain]**

*Switch to Streamlit "Ask the Codebase" tab. Type:*
```
what changed between versions and what might be affected?
```

Say (while agent responds):
> "The agent has four tools. Watch — it's going to chain two together. First `version_diff` reads the diff status from every node. Then it chains into `trace_impact` — that's a multi-hop graph traversal: what calls this function, and what calls *that*. Two hops through the calls graph in a single SurrealQL query."

*Point at tool calls appearing in the response*

Say:
> "That chain wasn't scripted — the LLM decided to assess the blast radius after seeing the diff. That's LangGraph conditional routing."

---

**[1:25 — LANGSMITH: show the trace]**

*Switch to LangSmith tab — find the trace for the query*

Say:
> "Every step is observable. Here's the LangGraph run — you can see the LLM reasoning, then version_diff fires, then trace_impact. Tool calls, arguments, results — fully auditable."

*Point at the tool call sequence in the trace tree*

---

**[1:35 — PERSISTENT STATE: interrupt and resume]**

*Point at the ingestion traces from seed setup (the httpx run should show in LangSmith). Or if time allows, describe the mechanism.*

Say:
> "The ingestion pipeline checkpoints after every single file — into SurrealDB. Kill the agent mid-run, restart with the same thread ID, it resumes from exactly where it stopped. Same database storing the graph, the vectors, AND the agent state."

---

**[1:45 — CLOSE: back to httpx graph + summary]**

*Switch back to Streamlit — select httpx, show the full graph one more time*

Say:
> "One database, four jobs: knowledge graph with typed edges, HNSW vector search with BM25 fusion, version-aware diffing, and LangGraph checkpoint state. SurrealDB does all of it. LangGraph routes the agent through conditional tool chains. LangSmith traces every step. Dead Reckoning — navigate any codebase."

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
- **Why it matters:** The agent can answer "what's been indexed?" without the user needing to check the UI
- **LangSmith:** Single retriever span querying the ingestion table
- **Prompt to trigger:** `"what repos are indexed?"` or `"what versions are available?"`

### Multi-tool chain (the demo moment)
- The prompt `"what changed between versions and what might be affected?"` triggers **version_diff** first, then the agent chains into **trace_impact** on the modified items
- This shows LangGraph's conditional tool routing: the agent reasons about the diff, then decides to assess the blast radius
- Visible as a two-step tool call sequence in LangSmith

---

## Pre-typed queries — scratch file, copy-paste during demo

```
# After v1->v2 diff (fixtures)
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
> "Context windows can't do multi-hop graph traversal. 'What calls this function, and what calls that?' is structural — you need the graph. And our search uses SurrealDB's native `search::rrf()` to fuse vector and keyword results inside the database, not in Python. LLMs also forget between sessions. Our agent's state persists across runs — conversation history, ingestion progress, version diffs — all in SurrealDB."

**"Why tar-based snapshots?"**
> "Same content-addressing principle as Docker image layers — SHA-256 per file, compared across versions. Pure Python stdlib (`tarfile` + `hashlib`), zero extra dependencies. Deletions are detected by set difference: if a file was in the old snapshot but not the new one, it's red."

**"How granular is the diff?"**
> "Function-level. We hash each function's source text and compare across versions. A modified file gets yellow, but inside it individual functions show green, yellow, or red — so you can see exactly which functions changed."

**"What happens to the graph on a new version?"**
> "We diff the two tar snapshots — old SHA-256 vs new. Same hash goes green, changed goes yellow, absent goes red. SurrealDB nodes get a `diff_status` field updated in place. The agent reads that field directly when answering 'what changed?'"

**"Why SurrealDB specifically?"**
> "One instance doing four things: knowledge graph with typed edges, HNSW vector search with BM25 full-text via native `search::rrf()`, LangGraph checkpoint state, and ingestion history with version tracking. Graph traversal AND vector search AND relational queries in the same SurrealQL statement. No second database."

**"What's LangGraph doing here?"**
> "Two things. First, the query agent is a LangGraph StateGraph with conditional edges — the LLM decides which tools to call, and chains them together. version_diff into trace_impact is a two-step reasoning chain, not hardcoded. Second, the ingestion pipeline is a separate LangGraph graph with per-file checkpoints — kill it, resume it, same thread ID. Both use the SurrealDB checkpointer from the `langgraph-checkpoint-surrealdb` package."

**"What does LangSmith show?"**
> "Every agent step is observable. Tool calls appear as nested spans — you can see the LLM reasoning, the SurrealQL queries, the graph traversals. The multi-tool chain (version_diff then trace_impact) is visible as a two-step sequence. Both the ingestion agent and query agent are fully traced. We use `@traceable` decorators on every retrieval function."

**"What's search::rrf()?"**
> "Reciprocal Rank Fusion. We run two searches — HNSW vector similarity for semantic meaning and BM25 for keyword matching — then SurrealDB's built-in `search::rrf()` function merges both ranked lists into one. The fusion happens inside the database in a single query, not in our Python code."

**"Does it work on non-Python repos?"**
> "Python only for the AST parsing — we used the built-in `ast` module. tree-sitter adds multi-language support; that's the obvious next step. The snapshot and diff layer works on any file type already."

**"Why httpx?"**
> "It's a well-known, well-structured Python HTTP library — 30+ files, hundreds of functions, real class hierarchies and call chains. Judges can recognise the code. It's big enough to show the graph is useful, small enough to ingest in under a minute."

---

## Scoring strategy — what maps where

| Demo moment | Criteria hit | Weight |
|---|---|---|
| Live SurrealQL query (RRF, graph traversal) | Structured Memory / Knowledge | 30% |
| Green/yellow/red diff + `diff_status` query | Structured Memory / Knowledge | 30% |
| Agent chains version_diff -> trace_impact | Agent Workflow Quality | 20% |
| Checkpoint resume (mention + LangSmith proof) | Persistent Agent State | 20% |
| v1 -> v2 versioned graph, ingestion records | Persistent Agent State | 20% |
| Codebase navigation + impact analysis | Practical Use Case | 20% |
| LangSmith trace walkthrough | Observability | 10% |

Every demo moment scores in at least one category. Nothing is filler.

---

## Timing failsafes

**If diff colouring is slow (> 5s):** Keep talking — "computing SHA-256 across both tar snapshots, comparing every file..." — it will arrive.

**If the conflict dialog doesn't appear:** v1 ingestion_id not in session state. Refresh, re-ingest v1 quickly (fixture repo is fast), then ingest v2.

**If the agent query is slow (> 15s):** Switch to LangSmith immediately — "you can watch it reasoning right now" — point at tool calls firing. The wait becomes part of the demo.

**If the SurrealQL terminal query fails:** Skip it, stay in the UI. The agent tool calls prove SurrealDB usage too. Don't debug live.

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

**If you have under 90 seconds:** Do: httpx graph (pan, "hundreds of nodes, real repo") -> v2 ingest live (green/yellow/red) -> agent query ("what changed and what might break?") -> close with "one database, four jobs". Skip terminal queries and LangSmith.

**The three things that must not fail:**
1. httpx graph rendering — impressive node count, visible call edges
2. Green/yellow/red colouring on the v1->v2 fixture diff
3. Agent multi-tool chain on "what changed and what might be affected?"

Rehearse all flows until they work perfectly every single time before Sunday morning.
