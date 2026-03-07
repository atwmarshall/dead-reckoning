# Development

Ordered build tasks. Work top to bottom. Do not move to the next task until the current one passes its success check.

Each task has a clear file target, what to build, and a test command. Run the test. See the output. Then move on.

---

## Prerequisites

```bash
pip install surrealdb langgraph langgraph-checkpoint-surrealdb \
            langchain-ollama python-dotenv \
            streamlit streamlit-agraph

# Ollama must be running locally with models pulled:
# ollama pull llama3.2:3b      # dev/testing (supports tools protocol)
# ollama pull gpt-oss:20b      # prod demo (supports tools protocol; thinking disabled via model_kwargs)
# ollama pull nomic-embed-text # embeddings
```

Read `langgraph-checkpoint-surrealdb` README before starting DEV-04:
```bash
pip show langgraph-checkpoint-surrealdb
# Find the source: pip show -f langgraph-checkpoint-surrealdb | grep Location
```

---

## Phase 1 — Parse (target: Fri night)

### DEV-01: Parse a single Python file

**File:** `ingestion/parser.py`

**Build:** A function `parse_file(path: str) -> dict` that uses the `ast` module to extract:
- `path` — the file path passed in
- `line_count` — total lines in the file
- `functions` — list of `{name, lineno, docstring}` dicts
- `classes` — list of `{name, lineno, bases}` dicts where `bases` is a list of base class name strings
- `imports` — list of strings: module names imported (`os`, `fastapi.Request`, `utils.helpers`)

For imports: handle both `import X` and `from X import Y` — store the top-level module or the full dotted path.

**Test:**
```bash
python -c "
from ingestion.parser import parse_file
r = parse_file('ingestion/parser.py')  # parse itself
print(r)
assert r['functions'], 'no functions found'
print('DEV-01 PASS — functions:', [f['name'] for f in r['functions']])
"
```

**Success:** Prints a dict with at least one function and one import. No exceptions.

---

### DEV-02: Parse a directory

**File:** `ingestion/parser.py` (extend)

**Build:** A function `parse_repo(repo_path: str) -> list[dict]` that walks a directory, finds all `.py` files, calls `parse_file` on each, returns a list of parsed dicts. Skip: `__pycache__`, `.git`, `venv`, `*.pyc`.

**Test:**
```bash
# Clone the demo repo first
git clone https://github.com/encode/httpx /tmp/demo-repo

python -c "
from ingestion.parser import parse_repo
results = parse_repo('/tmp/demo-repo')
print(f'Files parsed: {len(results)}')
fn_count = sum(len(r[\"functions\"]) for r in results)
print(f'Functions found: {fn_count}')
assert len(results) > 10, 'too few files'
assert fn_count > 50, 'too few functions'
print('DEV-02 PASS')
"
```

**Success:** > 10 files parsed, > 50 functions found, no exceptions on any file.

---

## Phase 2 — Schema + Load (target: Sat 10:00–11:30)

### DEV-03: Define SurrealDB schema

**File:** `ingestion/schema.surql`

**Build:** SurrealQL schema for: `file`, `function`, `class` tables with fields as defined in ARCHITECTURE.md. Edge tables: `imports`, `contains`. Indexes on `path` (unique) and `name+file` (unique).

**Test:**
```bash
# Apply schema to SurrealDB cloud
surreal import \
  --conn $SURREALDB_URL \
  --user $SURREALDB_USER \
  --pass $SURREALDB_PASS \
  --ns $SURREALDB_NS \
  --db $SURREALDB_DB \
  ingestion/schema.surql

# Then verify in SurrealDB cloud console:
# INFO FOR DB;
# -- should list: file, function, class, imports, contains tables
```

**Success:** `INFO FOR DB` shows all 5 tables defined. No errors on import.

---

### DEV-04: Load entities into SurrealDB

**File:** `ingestion/loader.py`

**Build:** Functions:
- `load_file(parsed: dict, db_client)` — upserts one file's entities and edges
- `get_db_client() -> SurrealDB client` — returns connected client using env vars

Use deterministic IDs from ARCHITECTURE.md. Use `INSERT INTO ... ON DUPLICATE KEY UPDATE` for upserts. After inserting nodes, `RELATE` the edges:
- `file -> contains -> function` for each function
- `file -> contains -> class` for each class
- `file -> imports -> file` for each local import (skip if the import isn't in the DB yet — best effort)

**Test:**
```bash
python -c "
from ingestion.parser import parse_file
from ingestion.loader import load_file, get_db_client

db = get_db_client()
parsed = parse_file('/tmp/demo-repo/httpx/_client.py')
load_file(parsed, db)
print('DEV-04 write PASS')
"

# In SurrealDB cloud console:
# SELECT * FROM file LIMIT 3;
# SELECT * FROM function LIMIT 3;
# SELECT ->contains->function.name FROM file LIMIT 1;
```

**Success:** File and function records appear in SurrealDB. Edge traversal query returns function names. Run twice — record count must not increase (idempotent).

---

### DEV-05: Seed the full demo repo

**File:** `ingestion/seed.py`

**Build:** CLI script that:
1. Takes `--repo` path argument
2. Gets list of files from `parse_repo`
3. Loops file by file, calling `load_file` for each
4. Prints progress: `[12/34] processing: _client.py`
5. On completion: prints total node counts

**Test:**
```bash
python ingestion/seed.py --repo /tmp/demo-repo

# Expected output:
# [1/34] processing: __init__.py
# [2/34] processing: _auth.py
# ...
# Done. Files: 34 | Functions: 287 | Classes: 42 | Edges: 310
```

**Success:** Completes without error. Node counts in SurrealDB match printed totals. Run twice — totals stay the same (idempotent).

---

## Phase 3 — Agent (target: Sat 11:30–13:30)

### DEV-06: Define agent state

**File:** `agent/state.py`

**Build:** `AgentState` TypedDict with `messages` (annotated with `add_messages`) and `repo_path` (str). See ARCHITECTURE.md for the exact definition.

**Test:**
```bash
python -c "
from agent.state import AgentState
from typing import get_type_hints
hints = get_type_hints(AgentState)
assert 'messages' in hints
assert 'repo_path' in hints
print('DEV-06 PASS')
"
```

**Success:** Imports without error. Both fields present.

---

### DEV-07: Build agent tools

**File:** `agent/tools.py`

**Build:** Four `@tool`-decorated functions. Each opens its own DB connection. Each returns a list of strings or a single string.

```
get_dependencies(module: str) -> list[str]
  Query: SELECT ->imports->file.path FROM file WHERE path CONTAINS module
  Returns: list of imported file paths

find_callers(function_name: str) -> list[str]  
  Query: SELECT <-calls<-function.name FROM function WHERE name = function_name
  Returns: list of function names that call this function
  (If calls edges don't exist yet, return empty list — not an error)

semantic_search(query: str) -> list[str]
  Embed the query using Ollama nomic-embed-text
  Query: SELECT name, docstring FROM function ORDER BY 
         vector::similarity::cosine(embedding, $vec) DESC LIMIT 5
  Returns: list of "function_name: docstring" strings

explain_module(module: str) -> str
  Query: get all functions in a file
  Return a formatted string listing each function + its docstring
```

**Test each tool standalone:**
```bash
python -c "
from agent.tools import get_dependencies, find_callers, explain_module

# Test with real data from your seeded DB
deps = get_dependencies.invoke({'module': '_client'})
print('deps:', deps)
assert isinstance(deps, list), 'must return list'

explained = explain_module.invoke({'module': '_auth'})
print('explain:', explained[:200])
assert len(explained) > 10, 'empty explanation'

print('DEV-07 PASS')
"
```

**Success:** Each tool returns real data from SurrealDB when called with a valid module name from the seeded repo.

---

### DEV-08: Wire the LangGraph agent

**File:** `agent/graph.py`

**Build:** 
1. `build_query_agent()` — returns compiled LangGraph agent with checkpointer
2. Nodes: `llm_node` (calls Ollama (llama3.2:3b or gpt-oss:20b) with tools bound), `tools_node` (runs tool calls)
3. Conditional edge: if response has tool calls → tools_node, else → END
4. Compile with `SurrealDBSaver` checkpointer

Check the `langgraph-checkpoint-surrealdb` package for correct init. Common pattern:
```python
# READ THE PACKAGE SOURCE — don't guess the API
# Location: pip show -f langgraph-checkpoint-surrealdb
```

**Test:**
```bash
python -c "
from agent.graph import build_query_agent
import os

agent = build_query_agent()
config = {'configurable': {'thread_id': 'test-query-001'}}

result = agent.invoke(
    {'messages': [('user', 'what files are in the repo?')], 
     'repo_path': '/tmp/demo-repo'},
    config
)
print(result['messages'][-1].content)
print('DEV-08 PASS')
"
```

**Success:** Agent returns a coherent response. LangSmith trace visible at smith.langchain.com with tool calls shown.

---

## Phase 4 — Interrupt/Resume (target: Sat 13:30–15:00)

### DEV-09: Build checkpointed ingestion agent

**File:** `ingestion/seed.py` (extend) or `agent/ingest_graph.py` (new)

**Build:** A LangGraph agent (separate StateGraph from query agent) for ingestion:

State:
```python
class IngestionState(TypedDict):
    messages: Annotated[list, add_messages]
    repo_path: str
    all_files: list[str]      # populated on first run
    processed_files: list[str]  # grows with each file processed
    current_file: str
```

Logic:
- On first invoke: populate `all_files` from `parse_repo`
- Each step: process next unprocessed file, update `processed_files`
- Checkpoint after each file (LangGraph does this automatically at each node)
- On resume (invoke with None): reload state, continue from where `processed_files` left off

Thread ID pattern: `f"ingest-{Path(repo_path).name}"`

**Test:**
```bash
# Step 1: start ingestion, kill it
python -c "
from agent.ingest_graph import build_ingestion_agent
agent = build_ingestion_agent()
config = {'configurable': {'thread_id': 'ingest-httpx'}}
agent.invoke({'repo_path': '/tmp/demo-repo', 'all_files': [], 'processed_files': [], 'current_file': ''}, config)
" &
sleep 5
kill %1

# Step 2: resume — check it continues, not restarts
python -c "
from agent.ingest_graph import build_ingestion_agent
agent = build_ingestion_agent()
config = {'configurable': {'thread_id': 'ingest-httpx'}}
result = agent.invoke(None, config)
print('processed so far:', len(result.get('processed_files', [])))
# Should be > 0 and show files that were already done before the kill
print('DEV-09 PASS')
"
```

**Success:** Second invocation shows processed_files count > 0 matching the files processed before the kill. Does not re-process already-done files. This is THE demo moment — it must be reliable.

---

### DEV-10: Add embeddings to functions

**File:** `ingestion/loader.py` (extend)

**Build:** After inserting each function, generate an Ollama nomic-embed-text embedding for its docstring (skip if docstring is None). Store in the `embedding` field. Use the `nomic-embed-text` model via Ollama.

Batch calls: don't embed one-by-one, collect docstrings per file and embed in one API call.

**Test:**
```bash
python -c "
from surrealdb import Surreal
import os, asyncio

async def check():
    db = Surreal(os.getenv('SURREALDB_URL'))
    await db.connect()
    await db.signin({'user': os.getenv('SURREALDB_USER'), 'pass': os.getenv('SURREALDB_PASS')})
    await db.use(os.getenv('SURREALDB_NS'), os.getenv('SURREALDB_DB'))
    
    result = await db.query(
        'SELECT name, embedding FROM function WHERE embedding != NONE LIMIT 3'
    )
    print(result)
    assert len(result[0]['result']) > 0, 'no embeddings found'
    print('DEV-10 PASS')

asyncio.run(check())
"
```

**Success:** At least some functions have non-null embedding arrays. `semantic_search` tool returns results.

---

## Phase 5 — UI (target: Sat 15:00–17:00)

### DEV-11: Streamlit app

**File:** `ui/app.py`

**Build:** Single-file Streamlit app with:

**Left sidebar:**
- Repo path text input
- "Ingest" button → triggers ingestion agent (show spinner)
- "Interrupt" button → stops ingestion (store thread status in `st.session_state`)
- Status display: "47 / 100 files indexed"

**Main area — two tabs:**

Tab 1: "Knowledge Graph"
- `streamlit-agraph` component showing nodes (files, functions) and edges
- Load data: `SELECT * FROM file LIMIT 50` + `SELECT * FROM contains LIMIT 200`
- Node colour: files = blue, functions = purple, classes = green

Tab 2: "Ask the Codebase"  
- Chat input at bottom
- Message history above
- On submit: invoke query agent with `thread_id = "query-{session_id}"`
- Stream response to chat

**Test:**
```bash
streamlit run ui/app.py
# Manual checks:
# 1. App loads without error
# 2. Graph tab shows nodes (requires seeded DB)
# 3. Type in chat: "what does _auth.py contain?" → gets real answer
# 4. LangSmith shows trace for that query
```

**Success:** All three manual checks pass. No Python exceptions in terminal.

---

## Phase 6 — Demo prep (target: Sat evening)

### DEV-12: Clean demo seed

**File:** `demo/seed_demo.py`

**Build:** Script that:
1. Drops and recreates the SurrealDB namespace cleanly (fresh state)
2. Re-applies schema from `schema.surql`
3. Runs full ingestion of `/tmp/demo-repo` with embeddings
4. Verifies counts
5. Prints: "Demo ready. Files: N | Functions: N | Classes: N"

**Test:**
```bash
python demo/seed_demo.py
# Should complete in under 90 seconds
# Final line: "Demo ready. Files: 34 | Functions: 287 | Classes: 42"
```

**Success:** Completes cleanly. Interrupt/resume scenario works after running this (test DEV-09 steps again).

---

### DEV-13: README + submission

**Files:** `README.md`, `requirements.txt`, `.env.example`

**Build:**
- `requirements.txt` — `pip freeze > requirements.txt` then trim to only direct deps
- `.env.example` — all env var names, no values, with comments
- README: verify quickstart instructions work from a clean clone (test on your own machine)

**Test:**
```bash
# In a fresh virtualenv:
pip install -r requirements.txt
cp .env.example .env
# Fill in real values
python ingestion/seed.py --repo /tmp/demo-repo
streamlit run ui/app.py
```

**Success:** Works end-to-end from a clean install with no extra guidance.

---

## Cut decisions

If time is short, cut in this order — lowest value first:

1. **`calls` edges** — skip function call graph, keep imports only
2. **`class` nodes** — skip class extraction, keep file + function only  
3. **`semantic_search` tool** — skip embeddings, keep 2 graph tools
4. **Streamlit graph viz** — replace with a text list of nodes (still functional)
5. **Interrupt button in UI** — just demo interrupt from terminal (manual kill)

**Never cut:**
- The interrupt/resume demo (DEV-09) — this is the wow moment
- LangSmith traces — 10% of judging score
- At least 2 working agent tools — needed for agent workflow score
- README install instructions — required for submission
