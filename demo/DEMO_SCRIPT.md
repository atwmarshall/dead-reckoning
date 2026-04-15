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
# One command: reset DB, ingest v1 fixtures only. Fast, clean, no conflicts.
uv run python demo/seed_demo.py

# Verify counts (should see 4 files, ~10 functions, 1 ingestion)
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

> **Note:** httpx ingestion is impressive but slow and creates conflicts with
> fixture data (v2 ingestion wipes httpx counts). For the live demo, start
> with v1 fixtures only — they're fast and sufficient. Ingest httpx via the
> UI live if you want the "wow" graph, or pre-ingest it separately for a
> static background graph that you don't modify during the demo.

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

    # Vector search (top-5 nearest neighbours among embedded functions)
    vs_rows = await db.query('''
        SELECT name, file.path AS path,
               vector::similarity::cosine(embedding, \$vec) AS score
        FROM \`function\`
        WHERE embedding IS NOT NONE AND embedding <|5,100|> \$vec
    ''', {'vec': vec})

    # BM25 keyword search
    ft_rows = await db.query('''
        SELECT name, file.path AS path,
               search::score(0) + search::score(1) AS score
        FROM \`function\`
        WHERE name @0@ \$keyword OR docstring @1@ \$keyword
        ORDER BY score DESC LIMIT 10
    ''', {'keyword': 'auth'})

    # RRF fusion (same algorithm as SurrealDB's search::rrf)
    scores, data_map = {}, {}
    k = 60
    for rank, r in enumerate((vs_rows or [])[:5]):
        name = r.get('name', '?')
        scores[name] = scores.get(name, 0) + 1 / (k + rank + 1)
        data_map[name] = r
    for rank, r in enumerate((ft_rows or [])[:10]):
        name = r.get('name', '?')
        scores[name] = scores.get(name, 0) + 1 / (k + rank + 1)
        if name not in data_map:
            data_map[name] = r

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    print('  RRF hybrid results (vector + BM25):')
    for name, score in ranked[:5]:
        path = data_map.get(name, {}).get('path', '?')
        print(f\"  {name:30s}  {path}\")
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
               <-calls<-\`function\`.name AS direct_callers,
               <-calls<-\`function\`<-calls<-\`function\`.name AS transitive_callers
        FROM \`function\`
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
Browser tab 1: Streamlit app (localhost:8501) — Knowledge Graph tab, v1 fixture selected
Browser tab 2: LangSmith (smith.langchain.com) — dead-reckoning project, recent traces
Terminal 1:    Streamlit running
Terminal 2:    Ready for SurrealQL demo queries (scratch queries above)
```

### State before judges arrive

- v1 sample fixture ingested — graph shows files, functions, classes, call edges
- v2 sample fixture NOT yet ingested — waiting to trigger live for the diff moment
- LangSmith has traces from seed runs visible
- SurrealQL scratch queries tested and working
- Docstring enrichment NOT run yet — will demo via the UI "Suggest Docstrings" button

---

## The script (90 seconds, tight)

> **Screen rule:** judges should always be looking at something interesting. Never leave them staring at a loading spinner — talk through it or switch screens.

---

### **[0:00–0:10] OPEN — v1 knowledge graph**

| Screen | Where to look | Why |
|--------|--------------|-----|
| Streamlit → **Knowledge Graph** tab | Main canvas — the graph visualisation | First impression: "this tool does something visual and useful" |

**What's on screen:** v1 fixture graph — files (large nodes), functions (small nodes), call edges (arrows), class groupings. Pre-loaded, no waiting.

**Your hands:** Mouse on the graph. Pan slowly left-to-right so judges see the full structure. Hover over a call edge to show the tooltip.

Say:
> "Every developer knows this moment — new codebase, no idea what talks to what. Dead Reckoning turns any Python repo into a queryable knowledge graph. Files, functions, classes, call relationships — all stored as nodes and edges in SurrealDB."

---

### **[0:10–0:25] QUERY — agent finds slugify + dependencies**

| Screen | Where to look | Why |
|--------|--------------|-----|
| Streamlit → **Ask the Codebase** tab | Chat input at bottom, then results area | Shows the agent is intelligent, not just a pretty graph |

**Your hands:** Click "Ask the Codebase" tab. The chat input is at the bottom. Type (pre-typed, just paste):

```
find the slugify function and what depends on it
```

Hit Enter. While the agent thinks (~3-5s), talk:

Say:
> "The agent uses hybrid search — vector similarity and BM25 keyword matching fused with Reciprocal Rank Fusion, all inside SurrealDB. Then it chains into trace_impact — a multi-hop graph traversal to find everything that depends on that function."

**Results appear** — function names, file paths, callers listed.

Say:
> "Structural reasoning — 'what calls X, and what calls that' — context windows can't do this. You need the graph."

**Don't linger.** As soon as results are visible, move on.

---

### **[0:25–0:50] DIFF — ingest v2 live, interrupt, resume**

| Screen | Where to look | Why |
|--------|--------------|-----|
| Streamlit → **Sidebar** (left panel) | Ingestion controls, quick-select buttons | Shows live pipeline + LangGraph interrupt — the "wow" moment |

**Your hands:** Move to the sidebar. Click the v2 quick-select button, then click **Ingest**.

Say:
> "Now watch what happens when code changes. Version one is indexed, let's ingest version two."

**[0:30]** Conflict dialog appears: "A previous version exists."

Say:
> "It detects the previous version automatically."

**Your hands:** Click **"Add new version"**. Ingestion runs — progress bar moves — then **pipeline pauses**.

| Screen | Where to look | Why |
|--------|--------------|-----|
| Streamlit → **Sidebar** | "Diff ready — review the graph, then click Resume" status + Resume button | Proves LangGraph interrupt + SurrealDB checkpoint persistence |

**[0:35]** The sidebar shows the green **Resume** button and diff log.

Say:
> "The ingestion pipeline just paused — a LangGraph interrupt. The agent checkpointed its state to SurrealDB. We could kill the process, come back tomorrow, and resume right here."

**Your hands:** Click **Resume**. Then immediately click the **Knowledge Graph** tab to watch it update.

| Screen | Where to look | Why |
|--------|--------------|-----|
| Streamlit → **Knowledge Graph** tab | Graph canvas — nodes changing colour | Visual payoff: green/yellow/red/blue = version-aware graph |

**[0:45]** Graph updates — nodes turn green (unchanged), yellow (modified), red (deleted), blue (new).

Say:
> "Green: unchanged. Yellow: modified. Red: deleted. Blue: new. Not just files — individual functions are diff'd. The knowledge graph is now version-aware."

---

### **[0:50–1:15] AGENT — 3-tool chain: diff → docstring → GitHub issue**

| Screen | Where to look | Why |
|--------|--------------|-----|
| Streamlit → **Ask the Codebase** tab | Chat input, then watch tool calls appear | The headline demo moment: autonomous multi-tool chain |

**Your hands:** Click "Ask the Codebase" tab. Paste:

```
What changed between versions? If anything new is undocumented, suggest a docstring and raise a GitHub issue.
```

Hit Enter. While the agent works (~10-15s), narrate the tool calls as they appear:

Say:
> "Watch the agent chain three tools. First — version_diff reads diff_status from the knowledge graph, spots new files, flags undocumented functions."

*version_diff result appears*

> "Then — generate_docstring reads the function source from SurrealDB and sends it to the LLM for a docstring."

*generate_docstring result appears*

> "Then — raise_issue files a GitHub issue with the suggestion. Three tools, one query. The agent decided the chain — LangGraph conditional routing."

*raise_issue result appears with GitHub URL*

**Your hands:** Point at / scroll to the GitHub issue URL in the response.

---

### **[1:15–1:25] LANGSMITH — show the trace**

| Screen | Where to look | Why |
|--------|--------------|-----|
| Browser tab 2 → **LangSmith** | Trace waterfall — 3-tool sequence | Proves observability: every step is auditable |

**Your hands:** Switch to the LangSmith browser tab (pre-opened). Click the most recent trace. The waterfall shows the 3-tool sequence.

Say:
> "Every step is observable. LLM reasoning, version_diff, generate_docstring, raise_issue — fully auditable in LangSmith."

**Point at** the 3 tool call spans in the waterfall. Don't click into them — just show the sequence.

---

### **[1:25–1:30] CLOSE — back to graph, summary**

| Screen | Where to look | Why |
|--------|--------------|-----|
| Streamlit → **Knowledge Graph** tab | Diff-coloured graph | End on the visual: version-aware knowledge graph |

**Your hands:** Switch back to Streamlit. Click Knowledge Graph tab. The diff-coloured graph is still visible.

Say:
> "One query: discovered a problem, generated a fix, filed an issue. SurrealDB stores the graph, vectors, diffs, and agent state. Dead Reckoning — navigate any codebase."

---

### **[1:30 — DONE]**

30 seconds of buffer for slow responses or judge questions during the demo.

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
# Before v2 ingestion — query the v1 graph
find the slugify function and what depends on it

# After ingesting v2 — the demo moment
What changed between versions? If anything new is undocumented, suggest a docstring and raise a GitHub issue.

# Backup queries if judges ask for more
what would break if I changed slugify?
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
| v1 graph + live agent query (hybrid search + trace_impact) | Practical Use Case | 20% |
| RRF hybrid search + graph traversal | Structured Memory / Knowledge | 30% |
| Green/yellow/red/blue diff + interrupt at diff review | Structured Memory / Knowledge | 30% |
| Agent chains version_diff → generate_docstring → raise_issue (3-tool chain) | Agent Workflow Quality | 20% |
| Agent chains hybrid_search → trace_impact | Agent Workflow Quality | 20% |
| Interrupt/resume at diff review (live) | Persistent Agent State | 20% |
| Per-file checkpointing + version ingestion records | Persistent Agent State | 20% |
| LangSmith trace walkthrough (3-tool chain visible) | Observability | 10% |

Every demo moment scores in at least one category. Nothing is filler. The 3-tool code review chain is the headline moment — discover, fix, file. Two separate agent query runs means two LangSmith traces to show.

---

## Timing failsafes

**If the agent query is slow (> 15s):** Say "the agent is reasoning now — let me show you what it's doing in LangSmith" and switch to LangSmith to show tool calls firing live. The wait becomes the observability demo.

**If diff colouring is slow (> 5s):** Keep talking — "computing SHA-256 across both tar snapshots, comparing every file..." — it will arrive.

**If the interrupt doesn't trigger (diff review):** The pipeline may skip straight through if there's no previous version detected. If this happens, don't stall — say "the pipeline checkpoints after every file into SurrealDB" and keep moving to the query.

**If the conflict dialog doesn't appear:** v1 ingestion_id not in session state. Refresh, re-ingest v1 quickly (fixture repo is fast), then ingest v2.

**If the SurrealQL terminal query fails:** Skip it, stay in the UI. The agent tool calls prove SurrealDB usage too. Don't debug live.

**If the agent returns "timed out while waiting for handshake response":** SurrealDB Cloud transient connection drop. Just resubmit the same query — it works on retry. Say "cloud database connection reset — retrying" and keep talking.

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

**If you have under 90 seconds:** Do: show v1 graph -> ask "find slugify and what depends on it" -> while waiting, ingest v2 (green/yellow/red) -> close with "one database, four jobs". Skip LangSmith and terminal queries.

**The three things that must not fail:**
1. v1 graph rendering + agent query against it
2. Green/yellow/red/blue colouring on the v1→v2 fixture diff
3. Agent 3-tool chain: version_diff → generate_docstring → raise_issue

Rehearse all flows until they work perfectly every single time before Sunday morning.

---

## OSS Contribution (mention during Q&A or close)

We found and fixed a bug in `langgraph-checkpoint-surrealdb` — the `setup()` method was a no-op that never created the checkpoint/write tables, causing a cryptic `string indices must be integers` error. PR is open:

https://github.com/lfnovo/langgraph-checkpoint-surrealdb/pull/2

Say (if judges ask about contributions, or drop it in the close):
> "We also found a bug in the SurrealDB checkpointer package and opened a fix — setup() wasn't creating the tables, so first-time users hit a cryptic error. The PR is up."
