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
# Quick connection test — should return a count or empty array, not an error
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

**Query 1 — Hybrid search on httpx (RRF fusion inside SurrealDB):**
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

**Query 2 — Multi-hop graph traversal on httpx (blast radius):**
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
        WHERE name CONTAINS \"_send\"
    ''')
    print('--- Blast radius for _send (httpx) ---')
    for r in (rows if isinstance(rows, list) else []):
        dc = r.get('direct_callers') or []
        tc = r.get('transitive_callers') or []
        print(f\"  {r.get('name')}: {len(dc)} direct callers, {len(tc)} transitive\")
    await db.close()

asyncio.run(impact())
"
```

**Query 3 — Diff status from versioned fixture graph:**
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

**[0:15 — QUERY HTTPX: agent on a real codebase]**

*Switch to "Ask the Codebase" tab (httpx still selected). Type:*
```
which functions handle authentication and what depends on them?
```

Say (while agent responds):
> "This is the agent running against the real httpx codebase — not a toy example. It's going to use hybrid search first — SurrealDB's `search::rrf()` fuses vector similarity and BM25 keyword matching in one query inside the database. Then it chains into trace_impact — a multi-hop graph traversal to find everything that depends on those auth functions."

*Results appear — real httpx function names, real file paths, real callers*

Say:
> "Real functions, real call chains, real impact analysis. This is structural reasoning — 'what calls X, and what calls that' — which context windows can't do. You need the graph."

---

**[0:45 — VERSIONED DIFF: switch to fixtures, ingest v2 live]**

*In sidebar, switch to the v1 fixture. Then trigger v2 ingestion — click the quick-select for v2, click Ingest.*

Say:
> "Now watch what happens when code changes. Here's a smaller repo — version one is indexed, let's ingest version two."

*Conflict dialog appears: "A previous version exists"*

> "It detects the previous version automatically."

*Click "Add new version" — ingestion runs — pipeline pauses at diff review (interrupt)*

Say:
> "The ingestion pipeline just paused. It's a LangGraph interrupt — the agent checkpointed its state to SurrealDB and is waiting for us to review the diff before continuing. This is resumable — we could kill the process, come back tomorrow, and it picks up right here."

*Click continue/resume — graph updates — nodes turn green, yellow, red, blue*

Say:
> "Green: unchanged. Yellow: modified. Red: deleted. Blue: new. Not just files — individual functions are diff'd. The knowledge graph is now version-aware."

---

**[1:05 — AGENT: automated code review chain]**

*Switch to "Ask the Codebase" tab. Type:*
```
What changed between versions? If anything new is undocumented, suggest a docstring and raise a GitHub issue.
```

Say (while agent responds):
> "Watch the agent chain three tools. First version_diff — reads diff_status from the knowledge graph, spots new files and flags undocumented functions. Then generate_docstring — reads the function source from SurrealDB, sends it to the LLM for a docstring. Then raise_issue — files a GitHub issue with the suggestion. Three tools, one query. The agent decided the chain — LangGraph conditional routing."

*Point at tool calls in the response — version_diff → generate_docstring → raise_issue. Show the GitHub issue URL.*

---

**[1:30 — LANGSMITH: show the trace]**

*Switch to LangSmith tab — find the trace for the query*

Say:
> "Every step is observable. LangGraph run — LLM reasoning, version_diff fires, generate_docstring chains in, raise_issue follows. Three tool calls, arguments, results — fully auditable in LangSmith."

*Point at the 3-tool call sequence. If time, scroll to show the httpx query trace too — two separate multi-tool chains visible.*

---

**[1:45 — CLOSE: back to httpx graph + summary]**

*Switch back to Streamlit — select httpx, show the full graph one more time*

Say:
> "One query: discovered a problem, generated a fix, filed an issue. SurrealDB stores the graph, vectors, diffs, and agent state. Dead Reckoning — navigate any codebase."

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
- **What it does:** Summarises what changed between two ingested versions at file AND function granularity, flags undocumented functions
- **SurrealDB feature:** Auto-detects versions from the `ingestion` table, then reads `diff_status` from the versioned knowledge graph, traverses `->contains->function` edges to show per-function changes within each file. New files/functions show as "added" status
- **Why it matters:** The graph is version-aware — not just "what exists" but "what changed" — and the agent can reason over the diff. Undocumented functions are flagged directly, giving the agent a natural signal to chain into `generate_docstring`
- **LangSmith:** Shows ingestion table query + diff_status graph queries as nested spans
- **Prompt to trigger:** `"what changed between versions?"` or `"show me the diff summary"`

### generate_docstring
- **What it does:** Generates a Python docstring for an undocumented function by reading its source from SurrealDB and sending it to the LLM
- **SurrealDB feature:** Stores full function source text in the knowledge graph — the agent queries it directly without filesystem access
- **Why it matters:** The agent discovers a problem (undocumented function via version_diff) and generates a fix — agentic code review
- **LangSmith:** Shows SurrealDB query for source + LLM call as nested spans
- **Prompt to trigger:** Chained automatically after version_diff flags undocumented functions

### raise_issue
- **What it does:** Creates a GitHub issue with the code improvement suggestion
- **Why it matters:** Completes the agentic loop — discover problem → reason about it → take action. The agent files an issue without human intervention
- **LangSmith:** Shows the `gh` CLI call as a tool span
- **Prompt to trigger:** Chained automatically after generate_docstring

### list_versions
- **What it does:** Shows all ingested repositories, their versions, file counts, timestamps, and snapshot status
- **SurrealDB feature:** Queries the `ingestion` table directly — demonstrates SurrealDB as a structured metadata store alongside its graph/vector roles
- **Why it matters:** The agent can answer "what's been indexed?" without the user needing to check the UI
- **LangSmith:** Single retriever span querying the ingestion table
- **Prompt to trigger:** `"what repos are indexed?"` or `"what versions are available?"`

### Multi-tool chain (the demo moment)
- The prompt `"What changed between versions? If anything new is undocumented, suggest a docstring and raise a GitHub issue."` triggers **version_diff** → **generate_docstring** → **raise_issue**
- This shows LangGraph's conditional tool routing: the agent discovers a problem, generates a fix, and files an issue — all autonomously
- Visible as a 3-step tool call sequence in LangSmith

---

## Pre-typed queries — scratch file, copy-paste during demo

```
# On httpx graph first (real-world, impressive)
which functions handle authentication and what depends on them?

# After switching to fixtures and ingesting v2 — the demo moment
What changed between versions? If anything new is undocumented, suggest a docstring and raise a GitHub issue.

# Backup queries if judges ask for more
what would break if I changed _send?
find the HTTP client logic and explain how requests flow
what repos have been indexed and how many versions?
what changed between versions and what might be affected?
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
> "Two things. First, the query agent is a LangGraph StateGraph with conditional edges — the LLM decides which tools to call, and chains them together. version_diff into trace_impact is a two-step reasoning chain, not hardcoded. Second, the ingestion pipeline is a separate LangGraph graph with per-file checkpoints and a human-in-the-loop interrupt at diff review — kill it, resume it, same thread ID. Both use the SurrealDB checkpointer."

**"What does LangSmith show?"**
> "Every agent step is observable. Tool calls appear as nested spans — you can see the LLM reasoning, the SurrealQL queries, the graph traversals. The multi-tool chain (version_diff then trace_impact) is visible as a two-step sequence. Both the ingestion agent and query agent are fully traced. We use `@traceable` decorators on every retrieval function."

**"What's search::rrf()?"**
> "Reciprocal Rank Fusion. We run two searches — HNSW vector similarity for semantic meaning and BM25 for keyword matching — then SurrealDB's built-in `search::rrf()` function merges both ranked lists into one. The fusion happens inside the database in a single query, not in our Python code."

**"How does the automated code review work?"**
> "The agent chains three tools. version_diff reads the knowledge graph and flags new files and undocumented functions. generate_docstring queries SurrealDB for the function's source code — we store the full source text in the graph — and sends it to the LLM to generate a docstring. raise_issue calls the GitHub CLI to create an issue with the suggestion. One query triggers all three — the agent decides the chain via LangGraph conditional routing."

**"Does it work on non-Python repos?"**
> "Python only for the AST parsing — we used the built-in `ast` module. tree-sitter adds multi-language support; that's the obvious next step. The snapshot and diff layer works on any file type already."

**"Why httpx?"**
> "It's a well-known, well-structured Python HTTP library — 30+ files, hundreds of functions, real class hierarchies and call chains. Judges can recognise the code. It's big enough to show the graph is useful, small enough to ingest in under a minute."

---

## Scoring strategy — what maps where

| Demo moment | Criteria hit | Weight |
|---|---|---|
| httpx graph + live agent query on real codebase | Practical Use Case | 20% |
| RRF hybrid search + graph traversal on httpx | Structured Memory / Knowledge | 30% |
| Green/yellow/red/blue diff + interrupt at diff review | Structured Memory / Knowledge | 30% |
| Agent chains version_diff → generate_docstring → raise_issue (3-tool chain) | Agent Workflow Quality | 20% |
| Agent chains hybrid_search → trace_impact on httpx | Agent Workflow Quality | 20% |
| Interrupt/resume at diff review (live) | Persistent Agent State | 20% |
| Per-file checkpointing + version ingestion records | Persistent Agent State | 20% |
| LangSmith trace walkthrough (3-tool chain visible) | Observability | 10% |

Every demo moment scores in at least one category. Nothing is filler. The 3-tool code review chain is the headline moment — discover, fix, file. Two separate agent query runs means two LangSmith traces to show.

---

## Timing failsafes

**If the httpx agent query is slow (> 15s):** Say "the agent is reasoning now — let me show you what it's doing in LangSmith" and switch to LangSmith to show tool calls firing live. The wait becomes the observability demo.

**If diff colouring is slow (> 5s):** Keep talking — "computing SHA-256 across both tar snapshots, comparing every file..." — it will arrive.

**If the interrupt doesn't trigger (diff review):** The pipeline may skip straight through if there's no previous version detected. If this happens, don't stall — say "the pipeline checkpoints after every file into SurrealDB" and keep moving to the query.

**If the conflict dialog doesn't appear:** v1 ingestion_id not in session state. Refresh, re-ingest v1 quickly (fixture repo is fast), then ingest v2.

**If the SurrealQL terminal query fails:** Skip it, stay in the UI. The agent tool calls prove SurrealDB usage too. Don't debug live.

**If Streamlit crashes entirely:**
```bash
uv run python -c "
from agent.graph import build_query_agent
agent = build_query_agent()
config = {'configurable': {'thread_id': 'demo-fallback'}}
r = agent.invoke({
    'messages': [('user', 'What changed between versions? If anything new is undocumented, suggest a docstring and raise a GitHub issue.')],
}, config)
print(r['messages'][-1].content)
"
```

**If you have under 90 seconds:** Do: httpx graph (pan, "real repo, hundreds of nodes") -> ask "which functions handle authentication and what depends on them?" -> while waiting, switch to fixtures, ingest v2 (green/yellow/red) -> close with "one database, four jobs". Skip LangSmith and terminal queries.

**The three things that must not fail:**
1. httpx graph rendering + agent query against it — proves real-world use case
2. Green/yellow/red/blue colouring on the v1->v2 fixture diff
3. Agent 3-tool chain: version_diff → generate_docstring → raise_issue

Rehearse all flows until they work perfectly every single time before Sunday morning.

---

## OSS Contribution (mention during Q&A or close)

We found and fixed a bug in `langgraph-checkpoint-surrealdb` — the `setup()` method was a no-op that never created the checkpoint/write tables, causing a cryptic `string indices must be integers` error. PR is open:

https://github.com/lfnovo/langgraph-checkpoint-surrealdb/pull/2

Say (if judges ask about contributions, or drop it in the close):
> "We also found a bug in the SurrealDB checkpointer package and opened a fix — setup() wasn't creating the tables, so first-time users hit a cryptic error. The PR is up."
